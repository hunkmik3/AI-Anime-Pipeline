"""Mechanical frame extraction and editorial assembly; no AI output inspection."""
import asyncio
import json
import math
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
        time_s=payload['time_s']
        if payload.get('at_end'):
            info=await probe(source)
            video=next(s for s in info['streams'] if s.get('codec_type')=='video')
            duration=float(video.get('duration') or info['format']['duration'])
            time_s=max(0,duration-2/int(payload.get('fps',24)))
        await command('ffmpeg','-v','error','-y','-i',source,'-ss',str(time_s),'-frames:v','1',image)
        if not image.exists():raise ValueError('Requested continuity frame is outside the generated clip.')
        data=image.read_bytes();url=await asyncio.to_thread(automation._publish,data)
        media=await asyncio.to_thread(automation._ingest_plate,data)
        if not url:raise ValueError('Continuity frame could not be published.')
        return {'images':[{'url':url,'reference_url':url,'media_id':media,'persisted':True}],
                'source_job':payload['source_job'],'time_s':time_s,'at_end':bool(payload.get('at_end'))}


async def assemble(payload):
    clips=payload['clips'];fps=int(payload['fps']);ratio=payload.get('aspect_ratio','16:9')
    if not clips:raise ValueError('No clips to assemble.')
    policy=payload.get('timing_policy','source_duration')
    if policy not in {'source_duration','full_take'}:raise ValueError('Unknown assembly timing policy.')
    a,b=map(float,ratio.split(':'));short=int(str(payload.get('resolution','720p')).rstrip('p'))
    width=round((short*a/b if a>=b else short)/2)*2
    height=round((short if a>=b else short*b/a)/2)*2
    target=Path(STORAGE_DIR)/'production-renders';target.mkdir(parents=True,exist_ok=True)
    name=str(uuid.uuid4())+'.mp4'
    with tempfile.TemporaryDirectory(prefix='flowboard-edit-') as folder:
        root=Path(folder);segments=[];receipts=[]
        for index,clip in enumerate(clips):
            source=root/f'{index}-source.mp4';out=root/f'{index}.mp4'
            await fetch(clip['url'],source);info=await probe(source)
            editorial=float(clip['duration_s']);duration=float(info['format']['duration'])
            if duration+1/fps<editorial:raise ValueError(f"{clip['sequence_key']}: generated clip is shorter than the editorial duration.")
            # Never assume provider padding is silent. Full-take mode retains
            # both audio and video, rounding UP to a whole output frame.
            seconds=math.ceil(duration*fps-0.0001)/fps if policy=='full_take' else editorial
            has_audio=any(s.get('codec_type')=='audio' for s in info['streams'])
            args=['ffmpeg','-v','error','-y','-i',str(source)]
            if not has_audio:args+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            args+=['-map','0:v:0','-map','0:a:0' if has_audio else '1:a:0',
                '-vf',f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={seconds}',
                '-af','apad','-t',str(seconds),'-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
                '-c:a','aac','-ar','48000','-ac','2',str(out)]
            await command(*args);segments.append(out)
            segment_info=await probe(out)
            receipts.append({'sequence_key':clip['sequence_key'],'source_job':clip['job_id'],
                'editorial_duration_s':editorial,'provider_duration_s':duration,
                'duration_s':float(segment_info['format']['duration'])})
        listing=root/'concat.txt';listing.write_text(''.join(f"file '{p.name}'\n" for p in segments))
        assembled=root/'film.mp4'
        # AAC packets need not end on a video frame; normalize the final PTS too.
        await command('ffmpeg','-v','error','-y','-f','concat','-safe','1','-i',listing,
                      '-vf',f'fps={fps},setsar=1','-c:v','libx264','-preset','fast','-crf','18',
                      '-c:a','copy','-movflags','+faststart',assembled)
        final_info=await probe(assembled)
        shutil.move(assembled,target/name)
    return {'filename':name,'duration_s':float(final_info['format']['duration']),'fps':fps,
            'width':width,'height':height,'timing_policy':policy,
            'editorial_duration_s':sum(float(c['duration_s']) for c in clips),'clips':receipts}
