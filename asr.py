#!/usr/bin/env python3
"""本地语音转写：读取 Alpha 解码后的 WAV，用 Qwen3-ASR 0.6B 输出文字。

由拾事后台按批调用，一次进程加载一次模型，结束即释放内存：
    python3 asr.py a.wav b.wav ...
标准输出只有一个 JSON：{"a.wav": "转写文本", ...}
"""
import array
import json
import os
import pathlib
import sys
import wave


def model_dir():
    return pathlib.Path(os.environ.get(
        'SHISHI_ASR_MODEL',
        str(pathlib.Path.home() / 'Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b')))


def main():
    paths = sys.argv[1:]
    if not paths:
        print('{}')
        return
    model = model_dir()
    files = [model / 'conv_frontend.onnx', model / 'encoder.int8.onnx', model / 'decoder.int8.onnx', model / 'tokenizer']
    if not all(p.exists() for p in files):
        print('{}')
        return
    try:
        import sherpa_onnx
        recognizer = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(model / 'conv_frontend.onnx'),
            encoder=str(model / 'encoder.int8.onnx'),
            decoder=str(model / 'decoder.int8.onnx'),
            tokenizer=str(model / 'tokenizer'),
            num_threads=4, decoding_method='greedy_search')
    except Exception:
        print('{}')
        return

    result = {}
    for path in paths:
        try:
            with wave.open(path, 'rb') as wav:
                rate, channels = wav.getframerate(), wav.getnchannels()
                raw = wav.readframes(wav.getnframes())
            samples = array.array('h')
            samples.frombytes(raw)
            if channels > 1:
                samples = samples[::channels]
            audio = [x / 32768.0 for x in samples]
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, audio)
            recognizer.decode_stream(stream)
            result[path] = (stream.result.text or '').strip()
        except Exception:
            result[path] = ''
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
