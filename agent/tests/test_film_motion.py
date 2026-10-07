import asyncio
import json
import pytest
from flowboard.services import film_motion, film_styles, prompt_writer, production_run


@pytest.mark.parametrize('style,medium,needle', [
    ('live_action_feature','live_action','continuous human'),
    ('anime_jp_modern','anime_jp','mainly on twos'),
    ('cartoon_us_2d','cartoon_us','brief purposeful smears'),
    ('donghua_premium','animation_3d','Continuous feature-animation'),
    ('cg3d','animation_3d','Continuous feature-animation'),
    ('anime','anime_jp','mainly on twos'),
    ('realistic','live_action','continuous human'),
])
def test_writer_uses_selected_medium_without_mutating_story(style,medium,needle):
    shots=[{'duration_s':4,'action':['Mara keeps the closed parcel.'],
            'dialogue':[{'who':'Mara','line':'Keep it closed.'}]}]
    original=json.dumps(shots)
    payload=prompt_writer._payload({},shots,[(0,4)],4,characters=[],environment=None,
        look=style,aspect_ratio='1:1',previous_state='Mara holds the parcel.',unsafe={},school_age=False)
    standard=payload['clip']['production_standard']
    assert standard['medium']==medium and needle in standard['cadence']
    assert json.dumps(shots)==original
    assert payload['opening_state']=='Mara holds the parcel.'
    assert payload['shots'][0]['dialogue'][0]['line']=='Keep it closed.'


def test_motion_change_invalidates_video_run_not_materials(monkeypatch):
    board={'style':'anime_jp_modern','nodes':[]}
    previous=production_run.input_version(board)
    image_version=film_styles.version('anime_jp_modern')
    monkeypatch.setitem(film_motion.STANDARDS['anime_jp'],'cadence','Revised directing cadence')
    assert production_run.input_version(board)!=previous
    assert film_styles.version('anime_jp_modern')==image_version
    assert film_motion.standard('custom-unknown')=={}


def test_independent_review_receives_medium_in_every_chunk(monkeypatch):
    seen=[]
    async def ask(system,user,*args,**kwargs):
        value=json.loads(user);seen.append(value)
        return {'status':'verified','findings':[],
                'checked_requirement_ids':[r['id'] for r in value['requirements']]}
    monkeypatch.setattr(prompt_writer.adapt_mod,'ask_json',ask)
    requirements=[{'id':f'q{i}','shot':1,'kind':'action','description':'holds parcel'} for i in range(65)]
    standard=film_motion.standard('anime_jp_modern')
    result=asyncio.run(prompt_writer.review_prompt('Prompt',requirements,[],[],
        staging_decisions=[],production_standard=standard))
    assert result['status']=='verified' and len(seen)==2
    assert all(x['production_standard']==standard for x in seen)
