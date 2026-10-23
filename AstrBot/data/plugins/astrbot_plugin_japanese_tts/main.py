"""Keep bilingual text replies while speaking only the Japanese dialogue."""

import copy
import asyncio
import time
import hashlib
from astrbot.api import logger
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.sources.gsv_selfhosted_source import ProviderGSVTTS


def spoken_text(text: str) -> str:
    """The persona contract puts the translation after this exact marker."""
    dialogue = text.split("【中文】", 1)[0].strip()
    if not dialogue:
        raise ValueError("Japanese dialogue is empty before the Chinese translation")
    return dialogue


@register_provider_adapter(
    provider_type_name="gsv_tts_japanese",
    desc="GPT-SoVITS: speak dialogue before 【中文】",
    provider_type=ProviderType.TEXT_TO_SPEECH,
)
class JapaneseGSVTTS(ProviderGSVTTS):
    def __init__(self, provider_config: dict, provider_settings: dict):
        super().__init__(provider_config, provider_settings)
        self._typed_post = provider_config.get('gsv_request_method') == 'POST'
        self._synthesis_lock=asyncio.Lock()
        self._typed_params = {
            key.removeprefix('gsv_'): copy.deepcopy(value)
            for key, value in provider_config.get('gsv_default_parms', {}).items()
        }
        if self._typed_post and not isinstance(self._typed_params.get('aux_ref_audio_paths', []), list):
            raise ValueError('POST auxiliary references must be a list')

    def build_synthesis_params(self, text: str) -> dict:
        if not getattr(self, '_typed_post', False):
            return super().build_synthesis_params(text)
        return {**copy.deepcopy(self._typed_params), 'text': text}

    async def _make_request(self, endpoint: str, params=None, retries: int = 3):
        if not getattr(self, '_typed_post', False) or endpoint != self.api_base+'/tts':
            return await super()._make_request(endpoint, params, retries)
        # The API's GET list parameter is a body field, and the base adapter stringifies
        # all defaults. POST preserves lists, booleans, path case and reference text.
        # A synthesis is attempted once; don't silently regenerate a different sample.
        async with self.get_session().post(endpoint, json=params) as response:
            if response.status != 200:
                raise RuntimeError(f'GPT-SoVITS synthesis failed with HTTP {response.status}')
            return await response.read()

    async def get_audio(self, text: str) -> str:
        spoken=spoken_text(text)
        started=time.monotonic()
        success=False
        try:
            # Serialise access to this voice's mutable inference cache.
            if not hasattr(self,'_synthesis_lock'):
                self._synthesis_lock=asyncio.Lock()
            async with self._synthesis_lock:
                path=await super().get_audio(spoken)
            success=True
            return path
        finally:
            logger.info('[voice_timing] synthesis_seconds=%.3f characters=%d input_sha=%s success=%s',
                        time.monotonic()-started,len(spoken),hashlib.sha256(spoken.encode()).hexdigest()[:12],success)


class JapaneseTTSPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
