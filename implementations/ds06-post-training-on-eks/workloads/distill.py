"""On-policy distillation (Design 06, "Distillation: off-policy and on-policy"), with TRL's DistillationTrainer.

The student writes the answers; the teacher scores every token the student chose, with the per-token reverse KL
between the two next-token distributions (beta = 1.0). Teacher and student share a tokenizer, which is checked
before training starts. The off-policy variant is not here: it is the SFT stage trained on teacher traces.
"""
import time

from datasets import Dataset
from transformers import AutoTokenizer
from trl import DistillationConfig, DistillationTrainer

from common import WORK, Tracker, finish, hf_callback, load_rows, model_path, register, resolved


def main():
    r = resolved()
    spec = r["spec"]
    hp = spec["hyperparameters"]
    tracker = Tracker("distill")
    student, teacher = model_path(r["base"]), model_path(r["teacher"])
    ts, tt = AutoTokenizer.from_pretrained(student), AutoTokenizer.from_pretrained(teacher)
    probe = "Step 1: 17 × 23 = 391. Final answer: \\boxed{391}"
    same_vocab = ts.get_vocab() == tt.get_vocab() and ts.encode(probe) == tt.encode(probe)
    tracker.event("tokenizers", same_vocabulary=same_vocab, student_vocab=len(ts), teacher_vocab=len(tt))
    if not same_vocab:
        raise SystemExit("teacher and student must share a tokenizer for per-token distillation")

    rows = []
    for name in spec["data"]["prompts"]:
        rows += [{"prompt": x["prompt"]} for x in load_rows(r["datasets"][name])]
    ds = Dataset.from_list(rows).shuffle(seed=int(spec.get("seed", 17)))
    args = DistillationConfig(
        output_dir=str(WORK / "out"), seed=int(spec.get("seed", 17)), beta=float(spec["objective"].get("beta", 1.0)),
        max_completion_length=int(spec["rollout"]["max_response_tokens"]), temperature=float(spec["rollout"].get("temperature", 1.0)),
        learning_rate=float(hp["lr"]), max_steps=int(hp["steps"]), per_device_train_batch_size=int(hp.get("batch_size", 8)),
        gradient_accumulation_steps=int(hp.get("grad_accum", 1)), warmup_steps=int(hp.get("warmup_steps", 0)),
        lr_scheduler_type="constant_with_warmup", bf16=True, gradient_checkpointing=True, logging_steps=1,
        save_strategy="no", report_to="none",
        use_vllm=bool(spec["rollout"].get("use_vllm", True)), vllm_mode="colocate",
        vllm_gpu_memory_utilization=float(spec["rollout"].get("colocate_gpu_memory", 0.25)),
        vllm_max_model_length=int(spec["rollout"].get("max_model_len", 1024)),
        teacher_model_init_kwargs={"dtype": "bfloat16"},
    )
    trainer = DistillationTrainer(model=student, teacher_model=teacher, train_dataset=ds, args=args, callbacks=[hf_callback(tracker)])
    t0 = time.time()
    out = trainer.train()
    wall = time.time() - t0
    trainer.save_model(str(WORK / "out"))
    losses = [x["loss"] for x in tracker.rows if "loss" in x]
    summary = {"steps": out.global_step, "wall_seconds": round(wall, 1), "loss_first": losses[0] if losses else None,
               "loss_last": losses[-1] if losses else None, "same_tokenizer": same_vocab, "teacher": r["teacher"]["ref"]}
    entry = register(str(WORK / "out"), r, summary)
    finish(tracker, {**summary, "registry_prefix": entry["model_prefix"]})


if __name__ == "__main__":
    main()
