"""Bounded music cleanup with native media and listening evidence retained."""
import asyncio
import json
import os
from pathlib import Path
import sys

from flowboard.config import STORAGE_DIR
from flowboard.services import production_media as media, production_audio, production_manifest as pm, reskin
from flowboard.services.video_analyzer.one_pass_film import atomic

VERSION = 1


def runtime():
    if os.getenv('FLOWBOARD_AUDIO_PYTHON'):
        return os.environ['FLOWBOARD_AUDIO_PYTHON']
    folder = Path(__file__).resolve().parents[2] / '.venv-audio'
    executable = folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return str(executable) if executable.exists() else sys.executable


async def clean(payload):
    root=Path(STORAGE_DIR)/'reskin-audio'/pm.digest(payload);root.mkdir(parents=True,exist_ok=True)
    receipt=root/'receipt.json';source=root/'native.mp4'
    if receipt.exists():return json.loads(receipt.read_text())
    if not source.exists():await media.fetch(payload['url'],source)
    info=await media.probe(source);duration=float(info['format']['duration'])
    has_audio=any(s.get('codec_type')=='audio' for s in info['streams'])
    native_sha=reskin.sha256(source)
    result={'native_url':payload['url'],'native_sha256':native_sha,'source_job':payload['source_job'],
            'native_preserved':True,'url':payload['url'],'method':'unchanged','limitations':[]}
    if not has_audio:
        result['limitations']=['Native clip has no audio; dialogue checking still applies.']
        atomic(receipt,result);return result
    audio=root/'native.wav'
    await media.command('ffmpeg','-v','error','-y','-i',source,'-vn','-ac','2','-ar','48000',audio)
    before=await production_audio.review(audio,root)
    result['listening_before']=before
    if before['background_music']=='absent':
        atomic(receipt,result);return result
    cleaned=root/'vocals.wav';output=root/'cleaned.mp4'
    if payload['has_dialogue']:
        if not cleaned.exists():
            temporary=root/'vocals.partial.wav'
            await media.command(runtime(),Path(__file__).parent/'reskin_workers'/'vocals.py',audio,temporary)
            await media.command('ffmpeg','-v','error','-i',temporary,'-f','null','-')
            temporary.replace(cleaned)
        result['method']='htdemucs_vocal_isolation'
        result['limitations']=['Foley/room tone can be reduced together with music.',
                              'Residual music/separation artifacts require final listening; cleanup does not certify success.']
    else:
        await media.command('ffmpeg','-v','error','-y','-f','lavfi','-i','anullsrc=r=48000:cl=stereo',
                            '-t',duration,cleaned)
        result['method']='silence_for_source_without_dialogue'
        result['limitations']=['Room tone and motivated sound effects were removed with unwanted music/voices.']
    await media.command('ffmpeg','-v','error','-y','-i',source,'-i',cleaned,'-map','0:v:0','-map','1:a:0',
        '-c:v','copy','-c:a','aac','-ar','48000','-ac','2','-b:a','160k','-t',duration,'-movflags','+faststart',output)
    await media.command('ffmpeg','-v','error','-i',output,'-f','null','-')
    # Content address the upload; the original provider file stays untouched.
    sha=reskin.sha256(output);staged=root/(sha+'.mp4')
    import shutil
    await asyncio.to_thread(shutil.copyfile,output,staged)
    from flowboard.services.flowstudio import r2
    result.update(url=await asyncio.to_thread(r2.upload_file,staged),sha256=sha,local_path=str(output))
    atomic(receipt,result);return result
