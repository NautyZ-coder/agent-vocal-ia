"""Appel depuis le navigateur (sans numéro de téléphone) : même agent, tuyau internet au lieu du réseau téléphonique.

Protocole WebSocket très simple entre la page /appel et le serveur :
  navigateur -> serveur : binaire = audio PCM 16 bits mono 16 kHz (micro)
  serveur -> navigateur : binaire = audio PCM 16 bits mono 16 kHz (voix de l'agent)
                          texte JSON {"type":"clear"} = le client a coupé la parole, vider la lecture
                          texte JSON {"type":"end"}   = l'agent a raccroché
"""

from __future__ import annotations

import json

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

WEB_SAMPLE_RATE = 16000


class WebPCMSerializer(FrameSerializer):
    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio or None
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "clear"})
        if isinstance(frame, (EndFrame, CancelFrame)):
            return json.dumps({"type": "end"})
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)) and len(data) >= 2:
            if len(data) % 2:
                data = data[:-1]
            return InputAudioRawFrame(audio=bytes(data), num_channels=1, sample_rate=WEB_SAMPLE_RATE)
        return None
