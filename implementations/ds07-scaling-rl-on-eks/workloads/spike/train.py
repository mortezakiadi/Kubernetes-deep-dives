import traceback
import trl.generation.weight_transfer as wt
_orig = wt.open_trainer_endpoint
def _traced(info):
    print("TRAINER ENDPOINT: opening", info, flush=True)
    try:
        r = _orig(info)
        print("TRAINER ENDPOINT: opened", flush=True)
        return r
    except BaseException:
        traceback.print_exc()
        print("TRAINER ENDPOINT: FAILED", flush=True)
        raise
wt.open_trainer_endpoint = _traced
from datasets import load_dataset
from trl import AsyncGRPOConfig, AsyncGRPOTrainer
from trl.rewards import accuracy_reward

def format_sample(sample):
    return {"prompt": [{"role": "user", "content": sample["question"]}], "solution": sample["answer"].split("####")[-1].strip()}

def main():
    dataset = load_dataset("openai/gsm8k", "main", split="train")
    dataset = dataset.map(format_sample, remove_columns=dataset.column_names)
    config = AsyncGRPOConfig(
        output_dir="/work/spike", save_strategy="no", per_device_train_batch_size=16, gradient_accumulation_steps=2,
        max_completion_length=1024, chat_template_kwargs={"enable_thinking": False}, max_steps=12, learning_rate=1e-5,
        report_to="none", vllm_server_base_url="http://rollout.rl-scale.svc:8000", logging_steps=1,
    )
    trainer = AsyncGRPOTrainer(model="Qwen/Qwen3-0.6B", args=config, train_dataset=dataset, reward_funcs=accuracy_reward)
    trainer.train()
    trainer.end()

if __name__ == "__main__":
    main()
