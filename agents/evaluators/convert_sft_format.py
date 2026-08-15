"""
Convert the terse SFT dataset into every format Bedrock accepts.

    python3 convert_sft_format.py

Reads dataset_terse_sft.jsonl and writes the same 106 records in each of the three
schemas, so whichever one the console demands is already on disk:

  dataset_terse_sft.jsonl              OpenAI chat completion, plain string content.
                                       Used by open-weight models such as Qwen
                                       through the OpenAI-compatible APIs.

  dataset_terse_converse.jsonl         schemaVersion bedrock-conversation-2024, the
                                       Converse schema. Used by Amazon Nova. The
                                       difference that breaks a naive copy is that
                                       content is an array of blocks, not a string.

  dataset_terse_prompt_completion.jsonl  {"prompt": ..., "completion": ...}. Used by
                                       the older text-to-text customization path.

Every writer validates what it produced by reading the file back and converting it
to the canonical form, so a silent shape error cannot reach an upload.
"""

import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(ROOT, "dataset_terse_sft.jsonl")

# Optional in the Converse schema, and it reinforces the one behaviour being
# taught. It carries no board data, in keeping with the rest of the dataset.
SYSTEM = "Answer with the shortest correct response. No preamble, no explanation."


def read_source():
    rows = []
    with open(SOURCE) as handle:
        for line in handle:
            if not line.strip():
                continue
            messages = json.loads(line)["messages"]
            user = next(m["content"] for m in messages if m["role"] == "user")
            assistant = next(m["content"] for m in messages if m["role"] == "assistant")
            rows.append((user, assistant))
    return rows


def to_converse(user, assistant):
    return {
        "schemaVersion": "bedrock-conversation-2024",
        "system": [{"text": SYSTEM}],
        "messages": [
            {"role": "user", "content": [{"text": user}]},
            {"role": "assistant", "content": [{"text": assistant}]},
        ],
    }


def to_prompt_completion(user, assistant):
    return {"prompt": user, "completion": assistant}


def to_openai(user, assistant):
    return {"messages": [{"role": "user", "content": user},
                         {"role": "assistant", "content": assistant}]}


def classify(assistant):
    """Infer the reference kind from the shape of the ideal reply."""
    if assistant.startswith("{"):
        return "json_schema", 500, 40
    if assistant == "I will not do that.":
        return "refusal", 400, 5
    if assistant == "Thanks":
        return "fixed_reply", 50, 1
    if assistant.replace("-", "").isdigit():
        return "numeric", 250, 4
    return "short_answer", 250, 2


def to_rft(user, assistant):
    """RFT rows carry the prompt only.

    The training container generates the assistant turn itself and appends it, so
    including a reply here would be scored instead of the model's own output. The
    expected answer moves into reference_answer, where the reward function reads it.
    """
    kind, points, ideal = classify(assistant)
    reference = {"kind": kind, "expected": assistant, "text": assistant,
                 "points": points, "ideal_tokens": ideal, "hearts": 1}
    if kind == "refusal":
        reference["must_not_contain"] = []
    return {"messages": [{"role": "user", "content": user}],
            "reference_answer": reference}


def canonical(record):
    """Reduce any of the schemas back to (user, assistant)."""
    if "prompt" in record:
        return record["prompt"], record["completion"]
    if "reference_answer" in record:
        return (record["messages"][0]["content"],
                record["reference_answer"]["expected"])
    messages = record["messages"]

    def text(entry):
        content = entry["content"]
        if isinstance(content, str):
            return content
        return "".join(block["text"] for block in content)

    user = next(text(m) for m in messages if m["role"] == "user")
    assistant = next(text(m) for m in messages if m["role"] == "assistant")
    return user, assistant


def write(name, rows, builder):
    path = os.path.join(ROOT, name)
    with open(path, "w") as handle:
        for user, assistant in rows:
            handle.write(json.dumps(builder(user, assistant)) + "\n")

    # Read back and prove the round trip, rather than trusting the writer.
    with open(path) as handle:
        lines = [line for line in handle if line.strip()]
    recovered = [canonical(json.loads(line)) for line in lines]
    assert len(lines) == len(rows), "%s: wrote %d lines for %d rows" % (name, len(lines), len(rows))
    assert recovered == rows, "%s: round trip changed the content" % name
    longest = max(len(line.encode()) for line in lines)
    print("  %-38s %3d records  %6d bytes  longest line %d"
          % (name, len(lines), os.path.getsize(path), longest))
    return path


def main():
    rows = read_source()
    print("source: %s  (%d records)" % (os.path.basename(SOURCE), len(rows)))
    if len(rows) < 100:
        raise SystemExit("source has %d records; Bedrock requires at least 100" % len(rows))
    print("wrote, each verified by reading it back and comparing to the source:")
    write("dataset_terse_sft.jsonl", rows, to_openai)
    write("dataset_terse_converse.jsonl", rows, to_converse)
    write("dataset_terse_prompt_completion.jsonl", rows, to_prompt_completion)
    write("dataset_terse_rft.jsonl", rows, to_rft)
    print()
    print("one record in each schema:")
    user, assistant = rows[0]
    for label, builder in (("openai", to_openai), ("converse", to_converse),
                           ("prompt_completion", to_prompt_completion)):
        print("  %-18s %s" % (label, json.dumps(builder(user, assistant))[:150]))


if __name__ == "__main__":
    main()
