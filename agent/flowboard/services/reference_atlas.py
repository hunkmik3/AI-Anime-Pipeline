"""Deterministic packing of approved references; source asset identity is unchanged."""
from copy import deepcopy
from io import BytesIO
import asyncio
import uuid
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from flowboard.services.production_manifest import digest

VERSION=1
MAX_CELLS=4
CELL=1536
HEADER=96


def plan(materials, reserved=0):
    """Keep character sheets separate when capacity permits; pack other sheets first."""
    slots=9-reserved
    if slots<1:raise ValueError('No image slots remain for materials.')
    units={}
    for key,m in sorted(materials.items()):
        source=m.get('reference_url') or 'missing:'+key
        unit=units.setdefault(source,{'url':m.get('reference_url',''),'bindings':[],'character':False})
        unit['bindings'].append({'material_key':key,'asset_id':m['asset_id'],'name':m['name'],'kind':m['kind']})
        unit['character'] |= m['kind']=='character'
    ordered=sorted(units.values(),key=lambda u:(u['character'],[b['asset_id'] for b in u['bindings']]))
    groups=[[u] for u in ordered]
    # Reduce only as much as needed. Never put more than four source images on a page.
    while len(groups)>slots:
        choices=[(i,j) for i in range(len(groups)) for j in range(i+1,len(groups)) if len(groups[i])+len(groups[j])<=MAX_CELLS]
        if not choices:raise ValueError('Too many references for readable four-cell atlases; split this clip.')
        i,j=min(choices,key=lambda pair:(sum(u['character'] for k in pair for u in groups[k]),len(groups[pair[0]])+len(groups[pair[1]]),pair))
        groups[i]+=groups.pop(j)
    pages=[]
    for group in groups:
        if len(group)==1:continue
        cells=[{'label':f'CELL {i+1}','position':['top left','top right','bottom left','bottom right'][i],
                'url':u['url'],'bindings':u['bindings']} for i,u in enumerate(group)]
        page={'schema_version':VERSION,'cell_pixels':CELL,'columns':2,'cells':cells}
        page['version']=digest(page);pages.append(page)
    return {'pages':pages,'source_images':len(units),'transport_images':len(groups),'reserved':reserved}


def compose(payload, images):
    cells=payload['cells']
    if not 2<=len(cells)<=4 or len(images)!=len(cells):raise ValueError('Atlas must contain two to four complete sources.')
    canvas=Image.new('RGB',(2*CELL,((len(cells)+1)//2)*(CELL+HEADER)),(246,246,246))
    draw=ImageDraw.Draw(canvas)
    try:font=ImageFont.truetype('DejaVuSans.ttf',36)
    except OSError:font=ImageFont.load_default(size=36)
    for i,(cell,blob) in enumerate(zip(cells,images)):
        with Image.open(BytesIO(blob)) as raw:
            image=ImageOps.exif_transpose(raw).convert('RGBA')
            background=Image.new('RGBA',image.size,'white');background.alpha_composite(image)
            fitted=ImageOps.contain(background.convert('RGB'),(CELL-32,CELL-32),Image.Resampling.LANCZOS)
        x=(i%2)*CELL;y=(i//2)*(CELL+HEADER)
        draw.rectangle((x,y,x+CELL-1,y+HEADER-1),fill=(25,30,45))
        # ASCII cell IDs avoid font/language ambiguity. Full asset names remain in the signed metadata.
        draw.text((x+24,y+24),cell['label'],font=font,fill='white')
        canvas.paste(fitted,(x+(CELL-fitted.width)//2,y+HEADER+(CELL-fitted.height)//2))
    out=BytesIO();canvas.save(out,format='PNG');return out.getvalue()


async def generate(payload):
    from flowboard.services import automation
    from flowboard.config import STORAGE_DIR
    blobs=await automation._fetch_reference_bytes([c['url'] for c in payload['cells']])
    png=await asyncio.to_thread(compose,payload,blobs)
    folder=Path(STORAGE_DIR)/'reference-atlases';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(payload['version']+'.png');path.write_bytes(png)
    url=await asyncio.to_thread(automation._publish,png)
    media_id=await asyncio.to_thread(automation._ingest_plate,png)
    if not url or not media_id:raise ValueError('Atlas needs a published URL and media identity before use.')
    return {'version':payload['version'],'url':url,'media_id':media_id,'local_path':str(path),
            'cells':payload['cells'],'width':2*CELL,'height':((len(payload['cells'])+1)//2)*(CELL+HEADER)}


def apply(materials, receipts):
    transformed=deepcopy(materials)
    seen=set()
    for receipt in receipts:
        out=receipt['result']
        for cell in out['cells']:
            for binding in cell['bindings']:
                key=binding['material_key']
                if key not in transformed or key in seen:raise ValueError('Atlas has missing/duplicate material binding.')
                m=transformed[key]
                if m['asset_id']!=binding['asset_id'] or m['reference_url']!=cell['url']:
                    raise ValueError('Atlas source image no longer matches material.')
                seen.add(key)
                m.update(reference_url=out['url'],media_id=out['media_id'],atlas_cell=cell['label'],
                    atlas_instruction=f"OUTER TRANSPORT ATLAS: {cell['label']} ({cell['position']}) contains the complete original reference sheet for {m['name']}. Any CELL coordinates in the original sheet description identify INNER cells within that sheet, not this outer atlas cell; locate the outer sheet first, then its specified inner object. These two coordinate levels may have different numbers. Preserve this asset's own design; do not mix objects or render either sheet, labels, borders or grid in the film.")
    return transformed


def verify(project_id, package, receipts, references):
    """A receipt must be a current server-produced composition, not a client URL assertion."""
    from flowboard.db import get_session
    from flowboard.db.models import AutomationJob
    verified=[]
    with get_session() as s:
        for supplied in receipts:
            try:j=s.get(AutomationJob,uuid.UUID(supplied['job_id']))
            except (ValueError,KeyError):raise ValueError('Invalid atlas job receipt')
            if not j or str(j.project_id)!=str(project_id) or j.kind!='atlas' or j.status!='succeeded':
                raise ValueError('Atlas job is not a completed job for this project.')
            if supplied.get('result')!=j.result:raise ValueError('Atlas receipt has been modified.')
            if j.result.get('version')!=j.payload.get('version') or j.result.get('cells')!=j.payload.get('cells'):
                raise ValueError('Atlas result does not match its source manifest.')
            verified.append(supplied)
    material=apply(package['materials'],verified)
    bound={r.get('source_asset_id') or r.get('id') or r.get('key'):r for r in references}
    for m in material.values():
        ref=bound.get(m['asset_id'],{})
        if ref.get('ref_url')!=m['reference_url'] or (m['media_id'] and ref.get('media_id')!=m['media_id']):
            raise ValueError('Atlas transport binding does not match '+m['asset_id'])
        if m.get('atlas_cell') and ref.get('atlas_cell')!=m['atlas_cell']:raise ValueError('Atlas cell label changed.')
    # Existing package verification still checks every original material.
    restored=deepcopy(references)
    originals={m['asset_id']:m for m in package['materials'].values()}
    for ref in restored:
        aid=ref.get('source_asset_id') or ref.get('id') or ref.get('key')
        if aid in originals:
            ref.update(ref_url=originals[aid]['reference_url'],media_id=originals[aid]['media_id'])
    return restored
