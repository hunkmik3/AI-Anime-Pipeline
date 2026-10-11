"""Local, restartable source-motion preparation; paid generation is elsewhere."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

from flowboard.config import STORAGE_DIR
from flowboard.services import reskin, production_media as media, production_manifest as pm


async def generate(payload, progress=None):
    source = reskin.source_path(payload['source_video_id'])
    if await asyncio.to_thread(reskin.source_fingerprint, source) != payload['source_sha256']:
        raise ValueError('Video nguồn đã thay đổi. Đối chiếu lại shotlist trước khi tạo motion reference.')
    root = Path(STORAGE_DIR) / 'reskin-motion' / pm.digest(payload)
    root.mkdir(parents=True, exist_ok=True)
    output = root / 'motion.mp4'; receipt = root / 'receipt.json'
    if receipt.exists():
        result = json.loads(receipt.read_text())
        if output.exists() and reskin.sha256(output) == result['sha256']:
            return result
        if output.exists(): output.replace(root / ('motion-corrupt-'+reskin.sha256(output)+'.mp4'))
    if not output.exists():
        temporary = root / 'motion.partial.mp4'
        worker = Path(__file__).parent / 'reskin_workers' / 'white_model.py'
        with (root / 'worker.log').open('wb') as log:
            render = [json.dumps(payload['render'])] if payload.get('render') else []
            process = await asyncio.create_subprocess_exec(sys.executable, '-u', str(worker), str(source),
                str(payload['start']), str(payload['end']), ','.join(map(str,payload['cuts'])), str(temporary), *render,
                stdout=asyncio.subprocess.PIPE, stderr=log)
            async def consume():
                async for line in process.stdout:
                    try: counter = json.loads(line)
                    except (ValueError, UnicodeDecodeError): continue
                    if progress: await progress(counter)
                await process.wait()
            try:
                await asyncio.wait_for(consume(),3600)
                if process.returncode:
                    raise ValueError('Không tạo được motion reference; xem reskin-motion worker.log (kiểm tra ffmpeg/model).')
            except BaseException:
                if process.returncode is None: process.kill()
                await process.wait(); raise
        await media.command('ffmpeg','-v','error','-i',temporary,'-f','null','-')
        temporary.replace(output)
    from flowboard.services import automation, media as library
    from flowboard.services.flowstudio import r2
    sha = reskin.sha256(output)
    staged = root / (sha + '.mp4')
    if not staged.exists():
        import shutil
        await asyncio.to_thread(shutil.copyfile, output, staged)
    url = await asyncio.to_thread(r2.upload_file, staged)
    media_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'flowboard:motion:' + sha))
    if not await asyncio.to_thread(library.ingest_inline_bytes, media_id, output.read_bytes(), kind='video', mime='video/mp4'):
        raise ValueError('Không lưu được video chuyển động vào thư viện KYC.')
    result = {'url': url, 'media_id': media_id, 'sha256': sha, 'input_digest': pm.digest(payload),
              'duration_s': payload['end']-payload['start'], 'source_sha256':payload['source_sha256'],
              'local_path':str(output), 'has_audio':False, 'method':'relative_depth_clay'}
    from flowboard.services.video_analyzer.one_pass_film import atomic
    atomic(receipt, result)
    return result
