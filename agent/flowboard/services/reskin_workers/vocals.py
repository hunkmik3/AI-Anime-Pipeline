"""Isolated optional Demucs runtime. Never synthesizes, rewrites or reorders speech."""
import sys
import torch
import soundfile as sf
from demucs.pretrained import get_model
from demucs.apply import apply_model
from demucs.separate import load_track


def main():
    torch.set_num_threads(4)
    model=get_model('htdemucs');model.cpu();model.eval()
    wav=load_track(sys.argv[1],model.audio_channels,model.samplerate)
    ref=wav.mean(0);mean=ref.mean();std=ref.std().clamp(min=1e-8)
    with torch.inference_mode():
        tracks=apply_model(model,((wav-mean)/std)[None],device='cpu',shifts=0,split=True,
                           overlap=.25,progress=False,num_workers=0)[0]
    vocals=(tracks[model.sources.index('vocals')]*std+mean).T.numpy()
    sf.write(sys.argv[2],vocals,model.samplerate,subtype='PCM_24')


if __name__=='__main__': main()
