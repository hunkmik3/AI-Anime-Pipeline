"""Local relative-depth export, one estimate per frame; no remote media upload.

Optional dependencies in a separate environment: torch, torchvision, av,
opencv-python-headless, numpy, huggingface-hub. FFmpeg/ffprobe must be on PATH.
Pass --model-code pointing to https://github.com/DepthAnything/Depth-Anything-V2
(checkpoint used commit recorded in docs/checkpoints/1011-2026-10-14.json).
Model weights download from the official Depth-Anything-V2-Small Hugging Face repo
on first use. No model code, weights, videos or local database are bundled here.

This is a standalone export tool, not an application route or generation step.
The output is relative inverse depth, not metric distance; near=white, far=black.
It retains source frame timestamps and writes a verification receipt. Single-frame
estimation can flicker; it does not certify source or generated-film fidelity.
"""
from pathlib import Path
from fractions import Fraction
import sys, json, time, hashlib, subprocess, argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True, help='Final film to estimate depth from')
parser.add_argument('--output', type=Path, required=True, help='New silent grayscale MP4; existing files are not overwritten')
parser.add_argument('--model-code', type=Path, required=True, help='Local DepthAnything/Depth-Anything-V2 checkout containing depth_anything_v2')
parser.add_argument('--work-dir', type=Path, help='Progress, receipt and visual samples; default: output basename + .depthmap')
parser.add_argument('--input-size', type=int, default=518)
parser.add_argument('--device', choices=['auto', 'mps', 'cuda', 'cpu'], default='auto')
args = parser.parse_args()
if not args.source.is_file():
    parser.error('--source must be an existing video')
if not (args.model_code / 'depth_anything_v2' / 'dpt.py').is_file():
    parser.error('--model-code must point to a Depth-Anything-V2 checkout')
if args.input_size <= 0:
    parser.error('--input-size must be positive')
R = (args.work_dir or args.output.with_suffix('.depthmap')).resolve()
R.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(args.model_code.resolve()))
import av
import cv2
import numpy as np
import torch
from huggingface_hub import hf_hub_download
from depth_anything_v2.dpt import DepthAnythingV2

SOURCE = args.source.resolve()
OUTPUT = args.output.resolve()
PARTIAL = R / (OUTPUT.stem + '.partial.mp4')
DEVICE = ('cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu') if args.device == 'auto' else args.device

def save(name, data):
    path = R / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temporary.replace(path)

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def main():
    assert not OUTPUT.exists(), 'Final output already exists; inspect its receipt first.'
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)
    cv2.setNumThreads(1)
    checkpoint = Path(hf_hub_download('depth-anything/Depth-Anything-V2-Small', 'depth_anything_v2_vits.pth'))
    model = DepthAnythingV2(encoder='vits', features=64, out_channels=[48, 96, 192, 384])
    model.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True))
    model = model.eval().to(DEVICE)
    src = av.open(str(SOURCE))
    ins = src.streams.video[0]
    ins.thread_type = 'SLICE'
    ins.codec_context.thread_count = 2
    target_count = ins.frames
    if not target_count:
        raise ValueError('Source must have a known frame count; remux to MP4 before export.')
    tb = ins.time_base
    dst = av.open(str(PARTIAL), 'w', options={'movflags': '+faststart', 'video_track_timescale': str(tb.denominator)})
    stream = dst.add_stream('libx264', rate=ins.base_rate or ins.average_rate or 24)
    stream.width, stream.height = ins.width, ins.height
    stream.pix_fmt = 'yuv420p'
    stream.time_base = tb
    stream.codec_context.time_base = tb
    stream.codec_context.thread_count = 2
    stream.options = {'crf': '12', 'preset': 'fast', 'bf': '0'}
    started = time.time()
    times, samples = [], []
    base_pts = None
    count = 0
    sample_indices = {24, 1440, 2880, 4320, 5760, 7200, 8400, 9360}
    save('progress.json', {'state': 'running', 'frames_done': 0, 'frames_total': target_count, 'source': str(SOURCE), 'device': DEVICE})
    with torch.inference_mode():
        for frame in src.decode(ins):
            if base_pts is None:
                base_pts = frame.pts
            bgr = frame.to_ndarray(format='bgr24')
            tensor, _ = model.image2tensor(bgr, input_size=args.input_size)
            depth = model(tensor.to(DEVICE))[0].cpu().numpy()
            assert np.isfinite(depth).all(), f'Non-finite depth at frame {count}'
            lo, hi = np.percentile(depth, [2, 98])
            normalized = np.clip((depth - lo) / max(float(hi - lo), 1e-6), 0, 1)
            normalized = cv2.resize(normalized, (ins.width, ins.height), interpolation=cv2.INTER_LINEAR)
            gray = np.rint(normalized * 255).astype(np.uint8)
            out = av.VideoFrame.from_ndarray(gray, format='gray')
            out.pts = frame.pts - base_pts
            out.time_base = tb
            out.duration = frame.duration
            for packet in stream.encode(out):
                dst.mux(packet)
            times.append(int(out.pts))
            if count in sample_indices:
                sample = R / f'sample-{count:05d}.jpg'
                comparison = np.hstack([bgr, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)])
                cv2.imwrite(str(sample), comparison, [cv2.IMWRITE_JPEG_QUALITY, 93])
                samples.append({'frame': count, 'time_s': float(out.pts * tb), 'path': str(sample)})
            count += 1
            if count % 120 == 0:
                elapsed = time.time() - started
                progress = {'state': 'running', 'frames_done': count, 'frames_total': target_count,
                            'percent': round(100 * count / target_count, 1), 'elapsed_s': round(elapsed, 1),
                            'fps_processing': round(count / elapsed, 2),
                            'estimated_remaining_s': round((target_count-count) * elapsed/count, 1)}
                save('progress.json', progress)
                print(json.dumps(progress), flush=True)
    for packet in stream.encode():
        dst.mux(packet)
    dst.close()
    src.close()
    assert count == target_count, (count, target_count)
    save('progress.json', {'state': 'verifying', 'frames_done': count, 'frames_total': target_count})
    check = av.open(str(PARTIAL))
    vs = check.streams.video[0]
    assert len(check.streams) == 1
    actual = []
    for f in check.decode(vs):
        actual.append(Fraction(f.pts) * f.time_base)
    check.close()
    assert len(actual) == count
    errors = [float(abs(a - Fraction(p) * tb)) for a, p in zip(actual, times)]
    assert max(errors) <= float(tb), max(errors)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(PARTIAL)]))
    PARTIAL.replace(OUTPUT)
    receipt = {
        'state': 'complete', 'source': str(SOURCE), 'source_sha256': sha(SOURCE),
        'output': str(OUTPUT), 'output_sha256': sha(OUTPUT),
        'model': 'Depth Anything V2 Small', 'model_source': 'https://github.com/DepthAnything/Depth-Anything-V2',
        'model_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.model_code, text=True).strip(),
        'checkpoint_sha256': sha(checkpoint), 'device': DEVICE, 'precision': 'float32',
        'inference_size': list(depth.shape[::-1]), 'output_size': [ins.width, ins.height],
        'frames': count, 'inference_stride': 1, 'audio': False,
        'depth_convention': 'relative inverse depth; white=near, black=far; not metric distances',
        'normalization': 'per-frame robust 2nd–98th percentile; no clay lighting, no face smoothing, no frame interpolation',
        'timing': 'Source video PTS copied with first timestamp rebased to zero; source internal gaps retained.',
        'source_first_pts': base_pts, 'source_time_base': str(tb), 'max_frame_timestamp_error_s': max(errors),
        'duration_s': float(probe['format']['duration']), 'file_size_bytes': int(probe['format']['size']),
        'verification': 'All frames decoded; frame count and every relative timestamp matched source.',
        'elapsed_s': round(time.time()-started, 2), 'samples': samples,
        'limitations': ['Single-image depth estimates may flicker in animated details and cannot measure real distances.',
                       'Depth is estimated from the final generated film, so its original content defects are unchanged.'],
        'model_code_path': str(args.model_code.resolve()),
        'model_code_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=args.model_code, text=True).strip())
    }
    save('receipt.json', receipt)
    save('frame-timestamps.json', {'time_base': str(tb), 'pts': times})
    save('progress.json', {'state': 'complete', 'frames_done': count, 'frames_total': count, 'output': str(OUTPUT), 'elapsed_s': receipt['elapsed_s']})
    print(json.dumps(receipt, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
