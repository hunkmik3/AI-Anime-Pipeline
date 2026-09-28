import pytest
from flowboard.services import authored_contract as authored, prompt_coverage as coverage, prompt_writer as writer


def fixture():
    assets=[{'id':'lead','reference_required':True},{'id':'crowd','reference_required':True}]
    shot={'id':'SH01','provenance':'authored_adaptation','character_keys':['lead'],
          'scene_present_asset_ids':['lead','crowd'],'asset_presence':[
          {'asset_id':'lead','visibility':'visible'}, {'asset_id':'crowd','visibility':'partial'}]}
    board={'adaptation':{'mode':'authored_adaptation'},'productionAssets':assets,
           'nodes':[{'data':{'kind':'sequence','sequence':{'key':'clip-01'},'shots':[shot]}}]}
    refs=[{'id':a['id'],'ref_label':f'@image{i+1}','ref_url':f'https://test/{i}'} for i,a in enumerate(assets)]
    return board,[shot],assets,refs


def test_authored_intent_has_no_source_frame_claim(monkeypatch):
    monkeypatch.setattr(authored.auth,'_server_secret',lambda:b'test-key')
    board,shots,assets,refs=fixture();report=authored.seal('project','Script',board)
    assert report['method']=='authored_script'
    assert 'evidence' not in report and 'review' not in report
    assert coverage.validate_source_contract(shots,assets,report,refs)==[]
    assert authored.current_project_issues('project','clip-01',board,'Script',report,shots)==[]


@pytest.mark.parametrize('defect',['forged','shot','asset','missing_reference','missing_presence','other_project','new_script'])
def test_authored_contract_rejects_tampering(monkeypatch,defect):
    monkeypatch.setattr(authored.auth,'_server_secret',lambda:b'test-key')
    board,shots,assets,refs=fixture();report=authored.seal('project','Script',board)
    if defect=='forged':report['signature']='bad'
    if defect=='shot':shots[0]['action']=['new action']
    if defect=='asset':assets[0]['name']='changed'
    if defect=='missing_reference':refs.pop()
    if defect=='missing_presence':
        shots[0]['asset_presence'].pop();report=authored.seal('project','Script',board)
    if defect in ['other_project','new_script']:
        assert authored.current_project_issues('other' if defect=='other_project' else 'project','clip-01',board,
                 'New script' if defect=='new_script' else 'Script',report,shots)
    else:assert coverage.validate_source_contract(shots,assets,report,refs)


def test_source_shots_cannot_be_sealed_as_authored():
    board,shots,_,_=fixture();shots[0]['source_shots']=[1]
    with pytest.raises(ValueError):authored.seal('project','Script',board)


def test_vietnamese_dialogue_labels_bind_to_correct_speaker():
    base='DURATION: 6 seconds.\n[SHOT 1 — 00:00–00:06]\n{}:\n“Hạ kiếm.”\n'
    options=dict(refs=[],duration=6,slots=[(0,6)],lines=[(1,'Hạ kiếm.')],exempt_shots=set(),school_age=False,
                 speakers=[(1,'MA TÔN','Hạ kiếm.')])
    assert writer.check_clip_prompt(base.format('MA TÔN'),**options)==[]
    assert writer.check_clip_prompt(base.format('ĐẠI CHƯỞNG GIÁO'),**options)
