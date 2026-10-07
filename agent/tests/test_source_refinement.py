import asyncio
import copy
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import source_inventory as inv, source_refinement as refine
from tests.test_source_inventory import _source, _draft


def _case(tmp_path):
    video,work,shots=_source(tmp_path)
    shots[0]['dialogue']='Original spoken line'
    shots[0]['source']['subtitle']='Original caption'
    inventory=_draft(shots);inventory['schema_version']=inv.SCHEMA_VERSION
    evidence=inv._initial_evidence(work,shots,30,False)
    report={'status':'needs_review','reviewed_shots':[1,2],'unresolved_shots':[1],
            'findings':[{'code':'source_description_mismatch','shot':1,'message':'Wrong visible action'}],
            'evidence':evidence}
    analysis={'video':{'fps':30},'shots':shots,'scene_inventory':inventory,
              'source_verification':report,'dialogue_track':[{'text':'Original spoken line'}],
              'transcript':{'segments':[{'text':'Original spoken line'}]}}
    return video,work,analysis


def _mock(monkeypatch, *, disposition='resolved', omit=False, caption=False, unconfirmed=False,
          first_issue=False, always_issue=False, fail_first=False):
    from flowboard.services.video_analyzer import source_asset_layers, source_entity_cleanup
    async def layers(inv,inventory,*args):return copy.deepcopy(inventory),[],{'moved_asset_ids':[]}
    async def entities(inv,inventory,*args,**kwargs):return copy.deepcopy(inventory),[],{'aliases':{}}
    monkeypatch.setattr(source_asset_layers,'classify_layers',layers)
    monkeypatch.setattr(source_entity_cleanup,'reconcile_entities',entities)
    calls=[];checks=[]
    async def complete(model,messages,**kwargs):
        payload=json.loads(messages[1]['content'][0]['text']);calls.append(payload)
        if fail_first and len(calls)==1:raise RuntimeError('provider unavailable')
        if 'targets' in payload:
            data={'observations':[{'shot':s['shot'],'evidence_ids':[f"shot-{s['shot']}-frame-1"],
                                   'people':['visible person']} for s in payload['targets']]}
        elif 'per_shot_inventory' in payload:
            data={'checks':[{'shot':s['shot'],'evidence_ids':[f"shot-{s['shot']}-frame-1"],
                             'findings':[]} for s in payload['proposed_source_shots']]}
        elif 'proposed_source_shots' in payload:
            checks.append(payload)
            rows=[]
            for s in payload['proposed_source_shots']:
                n=s['shot'];notes=['Visible person omitted'] if (always_issue or first_issue and len(checks)==1) and n==1 else []
                resolutions=[{'finding_id':f['finding_id'],'decision':disposition,'reason':'Current source frame supports this corrected fact',
                              'evidence_ids':[f'shot-{n}-frame-1']} for f in payload['prior_findings'] if f['shot']==n]
                rows.append({'shot':n,'status':'needs_review' if notes else 'verified','evidence_ids':[f'shot-{n}-frame-1'],
                             'findings':notes,'source_description_findings':[], 'resolutions':[] if omit else resolutions,
                             'confirmed_text_updates':{} if unconfirmed else payload.get('proposed_text_updates',{}).get(str(n),{})})
            data={'checks':rows,'review_requests':[]}
        else:
            data=copy.deepcopy(payload['draft_inventory'])
            data['visual_updates']=[{'shot':1,'source':{'action':'Corrected visible action',**({'subtitle':'New visible caption'} if caption else {})}}]
            data['review_requests']=[]
        return avis_text.Completion(text=json.dumps(data),model=model)
    monkeypatch.setattr(avis_text,'complete',complete)
    return calls,checks


def _run(case):return asyncio.run(refine.refine(*case))


def test_refinement_independent_resolution_preserves_speech_and_original_audit(tmp_path,monkeypatch):
    case=_case(tmp_path);original=copy.deepcopy(case[2]);calls,_=_mock(monkeypatch)
    result=_run(case)
    assert result['source_verification']['status']=='verified'
    assert result['shots'][0]['source']['action']=='Corrected visible action'
    assert result['shots'][0]['dialogue']=='Original spoken line'
    assert result['dialogue_track']==original['dialogue_track']
    assert result['transcript']==original['transcript']
    assert result['source_refinement_history'][0]['prior_report']==original['source_verification']
    assert case[2]==original
    assert len(calls)==4
    assert _run(case)==result
    assert len(calls)==4
    _run((case[0],case[1],result))
    assert len(calls)==4


def test_cached_source_frames_work_without_movie_and_restoration_invalidates(tmp_path,monkeypatch):
    case=_case(tmp_path);case[0].unlink();calls,_=_mock(monkeypatch)
    result=_run(case)
    assert result['source_verification']['status']=='verified'
    assert result['dialogue_track']==case[2]['dialogue_track']
    count=len(calls)
    _run((case[0],case[1],result))
    assert len(calls)==count
    case[0].write_bytes(b'restored original movie')
    _run((case[0],case[1],result))
    assert len(calls)>count


def test_missing_movie_does_not_waive_unresolved_visual_fact(tmp_path,monkeypatch):
    case=_case(tmp_path);case[0].unlink();_mock(monkeypatch,always_issue=True)
    result=_run(case)['source_verification']
    assert result['status']=='needs_review'
    assert 1 in result['unresolved_shots']


@pytest.mark.parametrize('change',['video','frame','model','transcript'])
def test_completed_result_is_invalidated_by_source_or_model_change(tmp_path,monkeypatch,change):
    case=_case(tmp_path);calls,_=_mock(monkeypatch);result=_run(case);count=len(calls)
    if change=='video':case[0].write_bytes(b'new source')
    elif change=='frame':(case[1]/case[2]['shots'][0]['frames'][0]).write_bytes(b'changed evidence')
    elif change=='transcript':result['transcript']['segments'][0]['text']='Changed narrative identity cue'
    else:monkeypatch.setattr(inv,'VERIFY_MODEL','different-model')
    _run((case[0],case[1],result))
    assert len(calls)>count


def test_missing_prior_dispositions_block_every_affected_shot(tmp_path,monkeypatch):
    case=_case(tmp_path);_mock(monkeypatch,omit=True)
    result=_run(case)['source_verification']
    assert result['status']=='needs_review'
    assert 1 in result['unresolved_shots']
    assert any(f['code']=='source_description_mismatch' and f['message']=='Wrong visible action' for f in result['findings'])
    assert any(a['active_finding']['code']=='resolution_incomplete' for a in result['issue_audit']['final'])


def test_new_round_one_defect_requires_final_disposition(tmp_path,monkeypatch):
    case=_case(tmp_path);_,checks=_mock(monkeypatch,first_issue=True)
    result=_run(case)
    assert len(checks)==2
    assert any(f['message']=='Visible person omitted' for f in checks[-1]['prior_findings'])
    assert any(r['finding']['message']=='Visible person omitted' for r in result['source_verification']['refinement']['resolutions'])


def test_provider_failure_can_resume_without_accepting_old_report(tmp_path,monkeypatch):
    case=_case(tmp_path);calls,_=_mock(monkeypatch,fail_first=True)
    failed=_run(case)
    assert failed['source_verification']['status']=='needs_review'
    assert failed['source_verification']['retryable']
    assert failed['shots']==case[2]['shots']
    result=_run(case)
    assert result['source_verification']['status']=='verified'
    assert len(calls)==5


@pytest.mark.parametrize('unconfirmed',[False,True])
def test_subtitle_changes_need_independent_exact_confirmation(tmp_path,monkeypatch,unconfirmed):
    case=_case(tmp_path);_mock(monkeypatch,caption=True,unconfirmed=unconfirmed)
    result=_run(case)
    assert result['shots'][0]['source']['subtitle']==('Original caption' if unconfirmed else 'New visible caption')
    assert result['shots'][0]['dialogue']=='Original spoken line'
    assert (result['source_verification']['status']=='verified') is not unconfirmed


@pytest.mark.parametrize('code',['identity_unresolved','source_mismatch','source_description_mismatch','registry_conflict'])
def test_sampling_exception_cannot_waive_concrete_visual_finding(tmp_path,monkeypatch,code):
    case=_case(tmp_path);case[2]['source_verification']['findings'][0]['code']=code
    _mock(monkeypatch,disposition='sampling_limit')
    result=_run(case)['source_verification']
    assert result['status']=='needs_review'
    assert 1 in result['unresolved_shots']
    assert any(f['code']==code and f['message']=='Wrong visible action' for f in result['findings'])
    assert any(a['active_finding']['code']=='resolution_incomplete' for a in result['issue_audit']['final'])


def test_unrelated_graphic_cannot_discharge_missing_person():
    batch=[{'shot':1}];evidence=[{'id':'one','shot':1}]
    ledger=[{'finding_id':'person-finding','shot':1,'asset_id':'person','code':'identity_unresolved'}]
    reply={'checks':[{'shot':1,'status':'verified','evidence_ids':['one'],'findings':[],
                     'screen_graphics_seen':[{'asset_id':'watermark','evidence_ids':['one']}],
                     'resolutions':[{'finding_id':'person-finding','decision':'screen_graphic','graphic_id':'watermark','reason':'Overlay','evidence_ids':['one']}]}]}
    _,findings,*_=refine._verify(reply,batch,evidence,[],ledger,{'watermark'}, {})
    assert any(f['code']=='resolution_incomplete' for f in findings)


def test_resolution_cannot_cite_foreign_shot_as_evidence():
    batch=[{'shot':1}];evidence=[{'id':'one','shot':1},{'id':'two','shot':2}]
    ledger=[{'finding_id':'f','shot':1,'code':'source_mismatch'}]
    reply={'checks':[{'shot':1,'status':'verified','evidence_ids':['one'],'findings':[],
                     'resolutions':[{'finding_id':'f','decision':'resolved','reason':'Fixed','evidence_ids':['one','two']}]}]}
    _,findings,*_=refine._verify(reply,batch,evidence,[],ledger,set(),{})
    assert any(f['code']=='resolution_incomplete' for f in findings)


@pytest.mark.parametrize('code',['audio_not_checked','continuous_motion_not_checked','scope_sampling'])
def test_ledger_exposes_sampling_disposition_only_for_explicit_scope_codes(code):
    finding={'code':code,'shot':1,'message':'Scope limitation'}
    row=refine._ledger([finding],[{'shot':1}])[0]
    assert set(row['allowed_decisions'])=={'resolved','unresolved','sampling_limit'}
    assert row['finding_id']==inv._digest(finding)[:24]
    assert 'allowed_decisions' not in finding


@pytest.mark.parametrize('code',['source_mismatch','source_description_mismatch','source_refinement_unresolved'])
def test_broad_or_mixed_graphic_findings_require_normal_cited_resolution(code):
    finding={'code':code,'shot':1,'asset_id':'branding','message':'Caption omitted and hand description incorrect'}
    row=refine._ledger([finding],[{'shot':1}],graphic_ids={'branding'})[0]
    assert row['allowed_decisions']==['resolved','unresolved']


def test_graphic_disposition_requires_explicit_confirmed_asset_binding_in_ledger():
    findings=[{'code':'registry_conflict','shot':1,'asset_id':'branding','message':'Graphic profile conflict'},
              {'code':'source_description_mismatch','shot':1,'message':'Visible graphic omitted'},
              {'code':'identity_unresolved','shot':1,'asset_id':'person','message':'Person not identified'}]
    rows=refine._ledger(findings,[{'shot':1}],graphic_ids={'branding'})
    assert set(rows[0]['allowed_decisions'])=={'resolved','unresolved','screen_graphic'}
    assert rows[1]['allowed_decisions']==rows[2]['allowed_decisions']==['resolved','unresolved']
    assert [r['finding_id'] for r in rows]==[inv._digest(f)[:24] for f in findings]


def test_ledger_recomputes_derived_guidance_without_changing_finding_identity():
    original={'code':'source_mismatch','shot':1,'message':'Missing visible person'}
    supplied={**original,'allowed_decisions':['sampling_limit','screen_graphic']}
    row=refine._ledger([supplied],[{'shot':1}],graphic_ids={'branding'})[0]
    assert row['allowed_decisions']==['resolved','unresolved']
    assert row['finding_id']==inv._digest(original)[:24]
    assert supplied['allowed_decisions']==['sampling_limit','screen_graphic']


def test_reviewer_sees_only_current_source_and_inventory(tmp_path, monkeypatch):
    case=_case(tmp_path); _,checks=_mock(monkeypatch,first_issue=True)
    _run(case)
    assert len(checks)==2
    for payload in checks:
        assert 'draft_inventory' not in payload
        assert 'source_shots' not in payload
        assert payload['proposed_source_shots'][0]['source']['action']=='Corrected visible action'
        assert 'identity_catalog' in payload


def test_remaining_scope_keeps_verified_shot_and_original_speech(tmp_path, monkeypatch):
    case=_case(tmp_path); calls,_=_mock(monkeypatch,always_issue=True)
    first=_run(case)
    assert first['source_verification']['unresolved_shots']==[1]
    before=copy.deepcopy(first['shots'][1]); calls,checks=_mock(monkeypatch)
    final=asyncio.run(refine.refine(case[0],case[1],first,only_unresolved=True))
    assert final['source_verification']['status']=='verified'
    assert final['source_verification']['reviewed_shots']==[1,2]
    assert final['source_verification']['refinement']['scope']['processed_shots']==[1]
    assert final['source_verification']['refinement']['scope']['retained_verified_shots']==[2]
    assert final['shots'][1]==before
    assert final['dialogue_track']==case[2]['dialogue_track']
    assert len(calls)==4
    assert checks[0]['fresh_observations']['observations'][0]['shot']==1
    assert [s['shot'] for s in checks[0]['proposed_source_shots']]==[1]


@pytest.mark.parametrize('changed',['frame','shot','inventory','model'])
def test_remaining_scope_does_not_reuse_changed_source(tmp_path, monkeypatch, changed):
    case=_case(tmp_path); _mock(monkeypatch,always_issue=True); first=_run(case)
    if changed=='frame':(case[1]/first['shots'][1]['frames'][0]).write_bytes(b'changed')
    elif changed=='shot':first['shots'][1]['source']['action']='Edited outside review'
    elif changed=='inventory':first['scene_inventory']['shots']['2']['asset_presence']=[]
    else:monkeypatch.setattr(inv,'VERIFY_MODEL','new-model')
    _,checks=_mock(monkeypatch)
    final=asyncio.run(refine.refine(case[0],case[1],first,only_unresolved=True))
    assert final['source_verification']['refinement']['scope']['mode']=='all'
    assert [s['shot'] for s in checks[0]['proposed_source_shots']]==[1,2]


def test_remaining_protocol_finding_includes_original_question(tmp_path,monkeypatch):
    case=_case(tmp_path);_mock(monkeypatch,omit=True);first=_run(case)
    _,checks=_mock(monkeypatch)
    asyncio.run(refine.refine(case[0],case[1],first,only_unresolved=True))
    questions=json.dumps(checks[0]['prior_findings'])
    assert 'Wrong visible action' in questions
    assert all('prior_question' not in f for f in checks[0]['prior_findings'])


def test_remaining_scope_rechecks_changed_group_dependency(tmp_path,monkeypatch):
    from flowboard.services.video_analyzer import source_issue_ledger
    case=_case(tmp_path);_mock(monkeypatch,always_issue=True);first=_run(case)
    _,checks=_mock(monkeypatch)
    original=source_issue_ledger.repair_inventory_references
    def cleanup(inventory,evidence):
        result,actions,findings=original(inventory,evidence)
        group=next(a for a in result['assets'] if a['id']=='market-group-b')
        group['member_ids']=['market-lead']
        return result,actions,findings
    monkeypatch.setattr(source_issue_ledger,'repair_inventory_references',cleanup)
    result=asyncio.run(refine.refine(case[0],case[1],first,only_unresolved=True))
    assert [s['shot'] for s in checks[0]['proposed_source_shots']]==[1,2]
    assert result['source_verification']['refinement']['scope']['retained_verified_shots']==[]


def test_alias_pending_reclassification_needs_matching_original_audit():
    old={'code':'entity_identity_unresolved','shot':2,'asset_id':'a','candidate_id':'b','message':'Candidate comparison uncertain'}
    true={'code':'identity_unresolved','shot':2,'asset_id':'a','candidate_id':'b','message':'Face cannot be identified here'}
    prior={'findings':[old,true],'refinement':{'identity_audit':{'after_new_assets':{'pairs':[{'pair':['b','a'],'status':'unresolved'}]}}}}
    blocking,notes=refine._classify_prior_alias_notes(prior)
    assert blocking==[true]
    assert notes[0]['original_finding']==old and notes[0]['blocking'] is False
    prior['refinement']['identity_audit']={}
    assert refine._classify_prior_alias_notes(prior)==([old,true],[])


def test_writer_echoed_proven_alias_cannot_create_duplicate_person(tmp_path,monkeypatch):
    case=_case(tmp_path)
    case[2]['scene_inventory']['identity_aliases']={'prior-person-id':'market-lead'}
    _mock(monkeypatch)
    original=avis_text.complete
    async def echo(model,messages,**kwargs):
        reply=await original(model,messages,**kwargs)
        payload=json.loads(messages[1]['content'][0]['text'])
        if 'draft_inventory' in payload:
            data=json.loads(reply.text)
            for row in data['shots'].values():
                for presence in row['asset_presence']:
                    if presence['asset_id']=='market-lead':presence['asset_id']='prior-person-id'
                    if presence.get('holder_id')=='market-lead':presence['holder_id']='prior-person-id'
            reply=avis_text.Completion(text=json.dumps(data),model=model)
        return reply
    monkeypatch.setattr(avis_text,'complete',echo)
    result=_run(case)
    assert result['source_verification']['status']=='verified'
    assert not any(a['id']=='prior-person-id' for a in result['scene_inventory']['assets'])
    for row in result['scene_inventory']['shots'].values():
        assert any(p['asset_id']=='market-lead' for p in row['asset_presence'])
        assert not any(p.get('holder_id')=='prior-person-id' for p in row['asset_presence'])
