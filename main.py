import time
from os import path

from loguru import logger

import whisperx
from whisperx.utils import write_txt, write_srt, format_timestamp

import stable_whisper
import whisper
from whisper.utils import WriteSRT, WriteJSON
import torch
from pathlib import Path
import argparse
from flask import Flask, request, jsonify
from threading import Thread
from enum import Enum

from typing import TextIO, Iterator


# 任务的状态
class TaskStatus(Enum):
    pending = 1
    running = 2
    finished = 3
    error = 4


# 任务的状态
class ASRs(Enum):
    whisper = 'whisper'
    whisperx = 'whisperx'
    stable_whisper = 'stable_whisper'


# 参数解析
parser = argparse.ArgumentParser()
# GPU ID
parser.add_argument("--gpu_id", default='0', type=int)
# 模型
parser.add_argument("--model", default='large', type=str)  # tiny base small medium large
# 启动的端口
parser.add_argument("--port", default='5000', type=int)
# http 接口的 token
parser.add_argument("--token", default='1234567890', type=str)
# 使用那个模型
# http 接口的 token
parser.add_argument("--asr", default=ASRs.whisper.value, type=str)
# 解析参数
args = parser.parse_args()
# 将arg_dict转换为dict格式
arg_dict = args.__dict__

app = Flask(__name__)
# 全局模型的实例
g_model = whisper.model
# 全局的任务字典
g_task_dic = {}
# 全局的任务列表
g_task_list = []
# http 接口的 token
g_token = arg_dict['token']
# 使用那个模型
g_asr_model = arg_dict['asr']
# "Seconds before and after to extend the whisper segments for alignment"
ALIGN_EXTEND = 2
# "Whether to clip the alignment start time of current segment to the end time of the last aligned word of the
# previous segment
ALIGN_FROM_PREV = True
# ["nearest", "linear", "ignore"], help="For word .srt, method to assign timestamps to non-aligned words,
# or merge them into neighbouring.")
INTERPOLATE_METHOD = "nearest"


# 语音识别任务的数据结构
class TranscribeData:
    def __init__(self, task_id: int, input_audio_full_path: str):
        self.task_id = task_id
        self.input_audio = input_audio_full_path
        self.task_status = TaskStatus.pending
        self.language = ""


@app.route('/')
def hello():
    return 'hello world!'


@app.route('/transcribe', methods=["GET", "POST"])
def transcribe():
    if request.headers.get('Authorization'):
        get_token = request.headers['Authorization']
        if get_token == "":
            return jsonify({"code": 400, "msg": "token error"})
        tokens = str.split(get_token, " ")
        if len(tokens) != 2:
            return jsonify({"code": 400, "msg": "token error"})
        if tokens[1] != g_token:
            return jsonify({"code": 400, "msg": "token error"})
    else:
        return jsonify({"code": 400, "msg": "token error"})

    if request.method == 'GET':
        # 获取任务的 id
        task_id = request.args.get("task_id")
        # 获取任务的状态
        task_status = get_task_status(int(task_id))
        if task_status is None:
            return jsonify({"code": 400, "msg": "task not found"})
        else:
            return jsonify({"code": 200, "status": str(task_status.value)})

    elif request.method == 'POST':
        # 获取任务
        # 获取 JSON 数据
        jdata = request.get_json()
        # 判断文件是否存在
        if not Path(jdata["input_audio"]).exists():
            return jsonify({"code": 400, "msg": "file not found"})
        tdata = TranscribeData(jdata["task_id"], jdata["input_audio"])
        add_task(tdata)
        return jsonify({"code": 200, "msg": "ok"})
    else:
        return jsonify({"code": 400, "msg": "error"})


# 添加任务到队列中
def add_task(data: TranscribeData):
    global g_task_dic, g_task_list

    if data.task_id in g_task_dic:
        return
    # 任务不存在，添加到任务字典中
    g_task_dic[data.task_id] = data
    # 任务不存在，添加到任务列表中
    g_task_list.append(data)


# 获取任务的状态
def get_task_status(task_id: int):
    global g_task_dic

    if task_id not in g_task_dic:
        return None
    # 任务存在
    status = g_task_dic[task_id].task_status
    if status != TaskStatus.pending and status != TaskStatus.running:
        # 如果 任务状态不是 pending 和 running，说明任务已经完成，删除任务
        del g_task_dic[task_id]
    return status


# 任务线程
def task_transcribe():
    global g_task_dic, g_task_list
    # 循环获取任务
    while True:

        if len(g_task_list) == 0:
            # 休眠1秒
            time.sleep(1)
            continue
        # 从队列中取出一个任务执行
        tan_data = g_task_list.pop(0)
        # 任务状态设置为运行中
        tan_data.task_status = TaskStatus.running
        # 更新任务的状态
        g_task_dic[tan_data.task_id] = tan_data

        logger.info("Transcription {task_id} start...", task_id=tan_data.task_id)

        # 检测文件是否存在
        if not Path(tan_data.input_audio).exists():
            logger.error("File {file} not found.", file=tan_data.input_audio)
            tan_data.task_status = TaskStatus.error
            g_task_dic[tan_data.task_id] = tan_data
            continue

        if tan_data.language == "":
            # 检测语言
            audio_lang = detect_language(tan_data.input_audio)
            logger.info("Transcription {task_id} use language {language}.",
                        task_id=tan_data.task_id, language=audio_lang)
            transcribe_result = g_model.transcribe(tan_data.input_audio,
                                                   language=audio_lang)
        else:
            # 使用提交的语言
            logger.info("Transcription {task_id} use language {language}.",
                        task_id=tan_data.task_id, language=tan_data.language)
            transcribe_result = g_model.transcribe(tan_data.input_audio,
                                                   language=tan_data.language)

        logger.info("Transcription {task_id} complete.", task_id=tan_data.task_id)
        # 构建输出的路径
        p = Path(tan_data.input_audio)
        out_srt = path.join(p.parent, p.stem + '.srt')
        if g_asr_model == ASRs.whisper.value:
            writer_srt = WriteSRT(str(p.parent))
            writer_json = WriteJSON(str(p.parent))
            writer_srt(transcribe_result, tan_data.input_audio)
            writer_json(transcribe_result, tan_data.input_audio)
        elif g_asr_model == ASRs.stable_whisper.value:
            # 写入文件
            stable_whisper.results_to_sentence_srt(transcribe_result, out_srt)
        elif g_asr_model == ASRs.whisperx.value:

            logger.info("Transcription {task_id} aligning...", task_id=tan_data.task_id)

            # load alignment model and metadata
            model_a, metadata = whisperx.load_align_model(language_code=transcribe_result["language"], device=device)
            # align whisper output
            result_aligned = whisperx.align(transcribe_result["segments"], model_a, metadata, tan_data.input_audio,
                                            device,
                                            extend_duration=ALIGN_EXTEND, start_from_previous=ALIGN_FROM_PREV,
                                            interpolate_method=INTERPOLATE_METHOD
                                            )
            logger.info("Transcription {task_id} aligned.", task_id=tan_data.task_id)

            with open(out_srt, "w", encoding="utf-8") as srt:
                whisperx_write_srt(result_aligned["segments"], file=srt)
        else:
            raise Exception("Unknown ASR model.")

        logger.info("Transcription {task_id} saved to {file}.", task_id=tan_data.task_id, file=out_srt)

        # 任务状态设置为完成
        tan_data.task_status = TaskStatus.finished
        # 更新任务的状态
        g_task_dic[tan_data.task_id] = tan_data


# 检测音频是什么语言
def detect_language(audio_file: str) -> str:
    logger.info("Detecting language...")
    # load audio and pad/trim it to fit 30 seconds
    audio = whisper.load_audio(audio_file)
    audio = whisper.pad_or_trim(audio)
    # make log-Mel spectrogram and move to the same device as the model
    mel = whisper.log_mel_spectrogram(audio).to(g_model.device)
    # detect the spoken language
    _, probs = g_model.detect_language(mel)
    logger.info(f"Detected language: {max(probs, key=probs.get)}")
    return max(probs, key=probs.get)


# whisperx 写 srt 的方法
def whisperx_write_srt(transcript: Iterator[dict], file: TextIO, spk_colors=None):
    """
    Write a transcript to a file in SRT format.
    Example usage:
        from pathlib import Path
        from whisper.utils import write_srt
        result = transcribe(model, audio_path, temperature=temperature, **args)
        # save SRT
        audio_basename = Path(audio_path).stem
        with open(Path(output_dir) / (audio_basename + ".srt"), "w", encoding="utf-8") as srt:
            write_srt(result["segments"], file=srt)
    """
    # spk_colors = {'SPEAKER_00':'white','SPEAKER_01':'yellow'}
    for i, segment in enumerate(transcript, start=1):
        # write srt lines

        text = f"{segment['text'].strip().replace('-->', '->')}"
        if spk_colors and 'speaker' in segment.keys():
            # f'<font color="{spk_colors[sentence.speaker]}">{text}</font>'
            text = f'<font color="{spk_colors[segment["speaker"]]}">{text}</font>'
        text += "\n"
        print(
            f"{i}\n"
            f"{format_timestamp(segment['start'], always_include_hours=True, decimal_marker=',')} --> "
            f"{format_timestamp(segment['end'], always_include_hours=True, decimal_marker=',')}\n"
            f"{text}",
            file=file,
            flush=True,
        )


if __name__ == '__main__':

    device = "cuda:" + str(arg_dict['gpu_id']) if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda"):
        logger.info("Using GPU: {gpu_id_name} ({gpu_id})",
                    gpu_id_name=torch.cuda.get_device_name(arg_dict['gpu_id']),
                    gpu_id=arg_dict['gpu_id'])
        # 设置是那个GPU
        torch.cuda.set_device(arg_dict['gpu_id'])
    else:
        logger.info("Using CPU")
    # 加载模型
    logger.info("Loading model: {mpdel_name} ...", mpdel_name=arg_dict['model'])

    if g_asr_model == ASRs.whisper.value:
        g_model = whisper.load_model(arg_dict['model'])
    elif g_asr_model == ASRs.stable_whisper.value:
        g_model = stable_whisper.load_model(arg_dict['model'])
    elif g_asr_model == ASRs.whisperx.value:
        g_model = whisperx.load_model(arg_dict['model'])
    else:
        raise Exception("Unknown ASR model.")

    logger.info("Whisper model loaded.")

    # 启动任务线程
    logger.info("Start task thread...")
    t1 = Thread(target=task_transcribe)
    t1.start()

    logger.info("Try start server...")
    # 启动服务
    app.run(host='0.0.0.0', port=arg_dict['port'])
