"""A gateway's uncached token count must not calibrate away cached evidence."""
import copy
import pytest
from agentlab.config import ProviderProfile,load_provider_profiles
from agentlab.providers.openai_compatible import normalize_usage


@pytest.mark.parametrize('uncached,read,written',[(1509,7343,322),(694,8752,1334),(9000,100,0),(1500,0,8000),(500,0,0)])
def test_explicit_additive_cache_schema_counts_all_input_without_mutating_wire_usage(uncached,read,written):
    usage=dict(prompt_tokens=uncached,completion_tokens=100,total_tokens=uncached+100,
               prompt_tokens_details=dict(cached_tokens=read,cache_write_tokens=written))
    original=copy.deepcopy(usage)
    normalized=normalize_usage(usage,'anthropic_cache_additive')
    assert normalized['prompt_tokens']==uncached+read+written
    assert normalized['total_tokens']==uncached+read+written+100
    assert normalized['prompt_tokens_details']['uncached_tokens']==uncached
    assert usage==original


def test_default_openai_usage_never_double_counts_cache():
    usage=dict(prompt_tokens=10000,completion_tokens=300,total_tokens=10300,
               prompt_tokens_details=dict(cached_tokens=8000))
    assert normalize_usage(usage)==usage
    assert normalize_usage({},'anthropic_cache_additive')=={}


def test_release_profiles_keep_mock_available_and_external_key_in_environment():
    profiles=load_provider_profiles()
    assert profiles['mock'].protocol == 'mock'
    assert profiles['mock'].secret_env is None
    external = profiles['openai_compatible']
    assert external.secret_env == 'VLM_API_KEY'
    assert external.base_url == 'https://api.example.com/v1'
    assert external.usage_schema == 'openai'


def test_unknown_usage_schema_is_rejected_before_a_request():
    with pytest.raises(ValueError,match='usage schema'):
        ProviderProfile(name='bad',protocol='openai_chat',model='model',usage_schema='guess')
