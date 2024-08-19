#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import aiohttp
import os
import websockets
import uuid
import json
import gzip
import copy
import resampy

from typing import Any, AsyncGenerator, Dict

from pipecat.frames.frames import AudioRawFrame, ErrorFrame, Frame, MetricsFrame, StartFrame
from pipecat.services.ai_services import TTSService
from pipecat.utils.audio import merge_pcm_list_into_mp3, change_audio_samplerate
from pipecat.utils.path import delete_file_by_prefix, parse_path

from loguru import logger

import numpy as np


MESSAGE_TYPES = {11: "audio-only server response", 12: "frontend server response",
                 15: "error message from server"}
MESSAGE_TYPE_SPECIFIC_FLAGS = {0: "no sequence number", 1: "sequence number > 0",
                               2: "last message from server (seq < 0)", 3: "sequence number < 0"}
MESSAGE_SERIALIZATION_METHODS = {0: "no serialization", 1: "JSON", 15: "custom type"}
MESSAGE_COMPRESSIONS = {0: "no compression", 1: "gzip", 15: "custom compression method"}


class VolcengineTTSService(TTSService):

    def __init__(
            self,
            *,
            app_id: str,
            token: str,
            voice_type: str,
            **kwargs):
        super().__init__(**kwargs)

        self.appid = app_id
        self.token = token
        self.cluster = "volcano_tts"
        self.voice_type = voice_type
        self.host = "openspeech.bytedance.com"
        self.api_url = f"wss://{self.host}/api/v1/tts/ws_binary"

        # version: b0001 (4 bits)
        # header size: b0001 (4 bits)
        # message type: b0001 (Full client request) (4bits)
        # message type specific flags: b0000 (none) (4bits)
        # message serialization method: b0001 (JSON) (4 bits)
        # message compression: b0001 (gzip) (4bits)
        # reserved data: 0x00 (1 byte)
        self.default_header = bytearray(b'\x11\x10\x11\x00')

        self.request_json = {
            "app": {
                "appid": self.appid,
                "token": "access_token",
                "cluster": self.cluster
            },
            "user": {
                "uid": "388808087185088"
            },
            "audio": {
                "voice": "other",
                "voice_type": "xxx",
                "encoding": "mp3",
                "speed_ratio": 1,
                "volume_ratio": 1,
                "pitch_ratio": 1
            },
            "request": {
                "reqid": "xxx",
                "text": "字节跳动语音合成",
                "text_type": "plain",
                "operation": "xxx"
            }
        }

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)

    async def set_voice(self, voice: str):
        logger.debug(f"Switching TTS voice to: [{voice}]")
        self._voice_id = voice

    async def run_tts(self, text, save_path=None, seg_size: int=0) -> AsyncGenerator[Frame, None]:
        tts_id = uuid.uuid4()
        save_path = parse_path("{project_root}/var/temp/%s.mp3" % tts_id)

        if save_path:
            file_prefix = save_path.replace(".mp3", "")
            delete_file_by_prefix(file_prefix)
        else:
            file_prefix = None

        submit_request_json = copy.deepcopy(self.request_json)
        submit_request_json["audio"]["voice_type"] = self.voice_type
        submit_request_json["request"]["reqid"] = str(uuid.uuid4())
        submit_request_json["request"]["operation"] = "submit"
        submit_request_json["request"]["text"] = text

        submit_request_json["audio"]["encoding"] = "pcm"

        payload_bytes = str.encode(json.dumps(submit_request_json))
        payload_bytes = gzip.compress(payload_bytes)  # if no compression, comment this line
        full_client_request = bytearray(self.default_header)
        full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
        full_client_request.extend(payload_bytes)  # payload
        # print("\n------------------------ test 'submit' -------------------------")
        # print("request json: ", submit_request_json)
        # print("\nrequest bytes: ", full_client_request)
        header = {"Authorization": f"Bearer; {self.token}"}
        idx = 0
        seg_list = []
        async with websockets.connect(self.api_url, extra_headers=header, ping_interval=None) as ws:
            await ws.send(full_client_request)
            while True:
                res = await ws.recv()

                seg_path = save_path.replace(".mp3", f".{idx}.pcm")
                file_to_save = open(seg_path, "ab")
                done = self.parse_response(res, file_to_save)
                file_to_save.close()

                if done or os.path.getsize(seg_path) > seg_size:
                    idx += 1
                    seg_list.append(seg_path)
                    # with open(seg_path, "rb") as fp:
                    #     audio_bytes = fp.read()

                    audio_np = np.fromfile(seg_path, dtype=np.int16)
                    if len(audio_np) > 0:
                        resampled_audio = resampy.resample(audio_np, 24000, 16000)
                        resampled_audio_bytes = resampled_audio.astype(np.int16).tobytes()

                        yield AudioRawFrame(resampled_audio_bytes, 16000, 1)

                if done:
                    merge_pcm_list_into_mp3(seg_list, save_path)
                    #logger.info("_process_one_submit done")
                    break
            # print("\nclosing the connection...")

    def parse_response(self, res, file):
        # print("--------------------------- response ---------------------------")
        # print(f"response raw bytes: {res}")
        protocol_version = res[0] >> 4
        header_size = res[0] & 0x0f
        message_type = res[1] >> 4
        message_type_specific_flags = res[1] & 0x0f
        serialization_method = res[2] >> 4
        message_compression = res[2] & 0x0f
        reserved = res[3]
        header_extensions = res[4:header_size*4]
        payload = res[header_size*4:]
        # print(f"            Protocol version: {protocol_version:#x} - version {protocol_version}")
        # print(f"                 Header size: {header_size:#x} - {header_size * 4} bytes ")
        # print(f"                Message type: {message_type:#x} - {MESSAGE_TYPES[message_type]}")
        # print(f" Message type specific flags: {message_type_specific_flags:#x} - {MESSAGE_TYPE_SPECIFIC_FLAGS[message_type_specific_flags]}")
        # print(f"Message serialization method: {serialization_method:#x} - {MESSAGE_SERIALIZATION_METHODS[serialization_method]}")
        # print(f"         Message compression: {message_compression:#x} - {MESSAGE_COMPRESSIONS[message_compression]}")
        # print(f"                    Reserved: {reserved:#04x}")
        if header_size != 1:
            # print(f"           Header extensions: {header_extensions}")
            pass
        if message_type == 0xb:  # audio-only server response
            if message_type_specific_flags == 0:  # no sequence number as ACK
                # print("                Payload size: 0")
                return False
            else:
                sequence_number = int.from_bytes(payload[:4], "big", signed=True)
                payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                payload = payload[8:]
                # print(f"             Sequence number: {sequence_number}")
                # print(f"                Payload size: {payload_size} bytes")
            file.write(payload)
            if sequence_number < 0:
                return True
            else:
                return False
        elif message_type == 0xf:
            code = int.from_bytes(payload[:4], "big", signed=False)
            msg_size = int.from_bytes(payload[4:8], "big", signed=False)
            error_msg = payload[8:]
            if message_compression == 1:
                error_msg = gzip.decompress(error_msg)
            error_msg = str(error_msg, "utf-8")
            logger.error(f"          Error message code: {code}")
            logger.error(f"          Error message size: {msg_size} bytes")
            logger.error(f"               Error message: {error_msg}")
            return True
        elif message_type == 0xc:
            msg_size = int.from_bytes(payload[:4], "big", signed=False)
            payload = payload[4:]
            if message_compression == 1:
                payload = gzip.decompress(payload)
            # print(f"            Frontend message: {payload}")
        else:
            # print("undefined message type!")
            return True

        return True
