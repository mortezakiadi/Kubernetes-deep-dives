"""Run inside the serving image: the defaults vLLM chooses for this GPU and model, read from the engine's own configuration object."""
from vllm.engine.arg_utils import EngineArgs
from vllm.usage.usage_lib import UsageContext
a = EngineArgs(model="Qwen/Qwen3-4B", dtype="half", max_model_len=4096)
c = a.create_engine_config(usage_context=UsageContext.OPENAI_API_SERVER)
s = c.scheduler_config
print("max_num_batched_tokens", s.max_num_batched_tokens, "max_num_seqs", s.max_num_seqs, "chunked", getattr(s, "enable_chunked_prefill", None), "async", getattr(s, "async_scheduling", None))
print("prefix_caching", c.cache_config.enable_prefix_caching)
