"""Mechanical frame extraction and editorial assembly; no AI output inspection."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import uuid
import httpx
from flowboard.config import STORAGE_DIR
from flowboard.services import automation


async def command(*args):
    proc=await asyncio.create_subprocess_exec(*map(str,args),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        stdout,stderr=await asyncio.wait_for(proc.communicate(),1800)
    except BaseException:
        if proc.returncode is None:proc.kill()
        await proc.wait();raise
    if proc.returncode:raise ValueError('Media processing failed: '+stderr.decode(errors='replace')[-1500:])
    return stdout


async def fetch(url, path):
    # These URLs come from persisted provider outputs, not arbitrary command arguments.
    if not url.startswith(('https://','http://')):raise ValueError('Media URL must be HTTP(S).')
    async with httpx.AsyncClient(timeout=120,follow_redirects=True) as client:
        async with client.stream('GET',url) as response:
            response.raise_for_status()
            with path.open('wb') as out:
                async for chunk in response.aiter_bytes():out.write(chunk)


async def probe(path):
    return json.loads(await command('ffprobe','-v','error','-show_streams','-show_format','-of','json',path))


async def extract_frame(payload):
    with tempfile.TemporaryDirectory(prefix='flowboard-frame-') as folder:
        root=Path(folder);source=root/'source.mp4';image=root/'frame.png'
        await fetch(payload['url'],source)
        await command('ffmpeg','-v','error','-y','-i',source,'-ss',str(payload['time_s']),'-frames:v','1',image)
        if not image.exists():raise ValueError('Requested continuity frame is outside the generated clip.')
        data=image.read_bytes();url=await asyncio.to_thread(automation._publish,data)
        media=await asyncio.to_thread(automation._ingest_plate,data)
        if not url:raise ValueError('Continuity frame could not be published.')
        return {'images':[{'url':url,'reference_url':url,'media_id':media,'persisted':True}],
                'source_job':payload['source_job'],'time_s':payload['time_s']}


async def assemble(payload):
    clips=payload['clips'];fps=int(payload['fps']);ratio=payload.get('aspect_ratio','16:9')
    if not clips:raise ValueError('No clips to assemble.')
    a,b=map(float,ratio.split(':'));short=int(str(payload.get('resolution','720p')).rstrip('p'))
    width=round((short*a/b if a>=b else short)/2)*2
    height=round((short if a>=b else short*b/a)/2)*2
    target=Path(STORAGE_DIR)/'production-renders';target.mkdir(parents=True,exist_ok=True)
    name=str(uuid.uuid4())+'.mp4'
    with tempfile.TemporaryDirectory(prefix='flowboard-edit-') as folder:
        root=Path(folder);segments=[]
        for index,clip in enumerate(clips):
            source=root/f'{index}-source.mp4';out=root/f'{index}.mp4'
            await fetch(clip['url'],source);info=await probe(source)
            seconds=float(clip['duration_s']);duration=float(info['format']['duration'])
            if duration+1/fps<seconds:raise ValueError(f"{clip['sequence_key']}: generated clip is shorter than the editorial duration.")
            has_audio=any(s.get('codec_type')=='audio' for s in info['streams'])
            args=['ffmpeg','-v','error','-y','-i',str(source)]
            if not has_audio:args+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            args+=['-map','0:v:0','-map','0:a:0' if has_audio else '1:a:0',
                '-vf',f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}',
                '-af','apad','-t',str(seconds),'-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
                '-c:a','aac','-ar','48000','-ac','2',str(out)]
            await command(*args);segments.append(out)
        listing=root/'concat.txt';listing.write_text(''.join(f"file '{p.name}'\n" for p in segments))
        assembled=root/'film.mp4'
        await command('ffmpeg','-v','error','-y','-f','concat','-safe','1','-i',listing,'-c','copy','-movflags','+faststart',assembled)
        shutil.move(assembled,target/name)
    return {'filename':name,'duration_s':sum(float(c['duration_s']) for c in clips),'fps':fps,
            'clips':[{'sequence_key':c['sequence_key'],'source_job':c['job_id'],'duration_s':c['duration_s']} for c in clips]}
