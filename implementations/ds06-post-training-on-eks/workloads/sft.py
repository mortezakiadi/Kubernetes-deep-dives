"""SFT stage (Design 06, "SFT: data mixtures and loss masking"), with TRL's SFTTrainer.

The recipe gives the sources and their weights; a mixture of a fixed size is drawn with a fixed seed, and the number
of examples actually drawn from each source is recorded. The loss is on assistant turns only, conversations are
packed without padding, and three properties are checked on the real batches and the real model before training:
which tokens carry loss, how full the packed rows are, and that a packed conversation cannot attend to its neighbour.
Off-policy distillation is this same stage with teacher-written traces as a source.
"""
import collections
import random
import time

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

from common import RUN_ID, WORK, Tracker, finish, hf_callback, load_rows, model_path, put_json, register, resolved


def ids_of(tok, messages, gen=False):
    out = tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=gen)
    return list(out["input_ids"] if isinstance(out, dict) or hasattr(out, "keys") else out)


def packing_isolation(base_dir, conv_a, conv_b):
    """Logits of conversation A alone, packed after B with restarting positions (what the trainer does), and packed
    after B as one long sequence (what would leak). In float32, so the first difference is numerical noise only.
    use_cache=False as in training: Transformers derives the per-conversation mask from the restarting position ids
    only when there is no KV cache and no attention mask."""
    tok = AutoTokenizer.from_pretrained(base_dir)
    m = AutoModelForCausalLM.from_pretrained(base_dir, dtype=torch.float32).cuda().eval()
    a, b = ids_of(tok, conv_a), ids_of(tok, conv_b)
    with torch.no_grad():
        alone = m(input_ids=torch.tensor([a]).cuda(), use_cache=False).logits[0]
        ids = torch.tensor([b + a]).cuda()
        packed = m(input_ids=ids, position_ids=torch.tensor([list(range(len(b))) + list(range(len(a)))]).cuda(), use_cache=False).logits[0, len(b):]
        leaky = m(input_ids=ids, position_ids=torch.tensor([list(range(len(b) + len(a)))]).cuda(), use_cache=False).logits[0, len(b):]
    res = {"tokens_a": len(a), "tokens_b": len(b), "max_abs_diff_packed": float((packed - alone).abs().max()),
           "max_abs_diff_one_sequence": float((leaky - alone).abs().max()), "attn_implementation": m.config._attn_implementation}
    del m
    torch.cuda.empty_cache()
    return res


def mask_report(trainer, tok):
    """Look at the first real batch the trainer will train on."""
    batch = next(iter(trainer.get_train_dataloader()))
    ids, labels = batch["input_ids"], batch["labels"]
    pos = batch.get("position_ids")
    trained = labels != -100
    starts = (pos == 0).nonzero().tolist() if pos is not None else []
    seg_end = starts[1][1] if len(starts) > 1 else ids.shape[1]
    first_ids, first_trained = ids[0, :seg_end], trained[0, :seg_end]
    return {"batch_tokens": int(ids.numel()), "tokens_with_loss": int(trained.sum()),
            "share_with_loss": round(float(trained.float().mean()), 3), "conversations_in_batch": len(starts) or ids.shape[0],
            "padding_tokens": int((ids == tok.pad_token_id).sum()) if pos is None else 0,
            "first_conversation_untrained_text": tok.decode(first_ids[~first_trained])[-220:],
            "first_conversation_trained_text": tok.decode(first_ids[first_trained])[:220]}


def main():
    r = resolved()
    spec = r["spec"]
    hp, data = spec["hyperparameters"], spec["data"]
    tracker = Tracker("sft")
    base_dir = model_path(r["base"])
    seed = int(spec.get("seed", 17))

    names = list(data["sources"])
    weights = [float(data["sources"][n]["weight"]) for n in names]
    probs = [w / sum(weights) for w in weights]
    rows = {n: load_rows(r["datasets"][n]) for n in names}
    # A mixture of a FIXED size: each slot picks a source with the recipe's probability, and each source is read in a
    # seeded order, restarting when it runs out. With interleave_datasets(stopping_strategy="all_exhausted") the
    # number of examples would depend on the weights, so an ablation of one weight would also change the data size.
    size = int(data["mixture_size"])
    rng = random.Random(seed)
    order = {n: rng.sample(range(len(rows[n])), len(rows[n])) for n in names}
    cursor = collections.Counter()
    picked = []
    for n in rng.choices(names, weights=probs, k=size):
        picked.append({"messages": rows[n][order[n][cursor[n] % len(rows[n])]]["messages"], "source": n})
        cursor[n] += 1
    mix = Dataset.from_list(picked)
    drawn = collections.Counter(mix["source"])
    mixture = {n: {"weight": round(p, 3), "rows_available": len(rows[n]), "drawn": drawn[n], "share_drawn": round(drawn[n] / size, 3),
                   "passes_over_source": round(drawn[n] / len(rows[n]), 2)} for n, p in zip(names, probs)}
    tracker.event("mixture", size=size, sources=mixture)
    conv_a = next(row["messages"] for row in mix if row["source"] == names[0])
    conv_b = next(row["messages"] for row in mix if row["source"] != names[0])
    mix = mix.remove_columns(["source"])

    iso = packing_isolation(base_dir, conv_a, conv_b)
    tracker.event("packing_isolation", **iso)

    args = SFTConfig(
        output_dir=str(WORK / "out"), seed=seed, data_seed=seed,
        assistant_only_loss=bool(spec["objective"].get("assistant_only_loss", True)),
        packing=bool(data.get("packing", True)), max_length=int(data.get("max_length", 1024)),
        learning_rate=float(hp["lr"]), num_train_epochs=float(hp.get("epochs", 1)), max_steps=int(hp.get("max_steps", -1)),
        per_device_train_batch_size=int(hp.get("batch_size", 8)), gradient_accumulation_steps=int(hp.get("grad_accum", 1)),
        warmup_steps=int(hp.get("warmup_steps", 5)), lr_scheduler_type="cosine", weight_decay=0.0,
        bf16=True, logging_steps=int(hp.get("logging_steps", 5)), save_strategy="no", report_to="none",
        include_num_input_tokens_seen=True, dataloader_num_workers=0,
    )
    trainer = SFTTrainer(model=base_dir, train_dataset=mix, args=args, callbacks=[hf_callback(tracker)])
    tok = trainer.processing_class
    masks = mask_report(trainer, tok)
    tracker.event("loss_mask", **masks)

    t0 = time.time()
    out = trainer.train()
    train_s = time.time() - t0
    trainer.save_model(str(WORK / "out"))

    # The template the model is served with must render prompts exactly as the template it was trained with.
    base_tok, saved_tok = AutoTokenizer.from_pretrained(base_dir), AutoTokenizer.from_pretrained(str(WORK / "out"))
    probe = conv_a[:-1]
    same_template = base_tok.apply_chat_template(probe, tokenize=False, add_generation_prompt=True) == \
        saved_tok.apply_chat_template(probe, tokenize=False, add_generation_prompt=True)
    losses = [row["loss"] for row in tracker.rows if "loss" in row]
    summary = {"mixture": mixture, "loss_first": losses[0] if losses else None, "loss_last": losses[-1] if losses else None,
               "steps": out.global_step, "train_seconds": round(train_s, 1), "tokens_seen": trainer.state.num_input_tokens_seen,
               "tokens_per_second": round(trainer.state.num_input_tokens_seen / train_s, 1), "loss_mask": masks,
               "packing_isolation": iso, "serving_template_matches_training": same_template}
    entry = register(str(WORK / "out"), r, summary)
    finish(tracker, {**summary, "registry_prefix": entry["model_prefix"]})


if __name__ == "__main__":
    main()
