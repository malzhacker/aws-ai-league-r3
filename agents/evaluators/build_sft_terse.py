"""
Build an SFT dataset that teaches one behaviour: answer, then stop.

    python3 build_sft_terse.py

Writes dataset_terse_sft.jsonl in the OpenAI chat completion format that Bedrock
expects for open-weight models: one JSON object per line, messages with plain
string content.

Two deliberate constraints:

  * At least 100 records. Bedrock rejects a smaller training set, and the datasets
    already in this directory hold 42, 13 and 9 rows, so none of them can be used
    for a customization job on its own.
  * No board data of any kind. No tile ids, no map, no door codes, no key strings.
    The custom models are registered and never invoked, so their content cannot
    affect a single point, which makes carrying board data into them pure
    disqualification risk for zero upside.

What the model is taught is style, not answers: the shortest correct reply, no
preamble, no restatement, no trailing offer of further help.
"""

import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "dataset_terse_sft.jsonl")


# --------------------------------------------------------------------------- #
# One-word factual answers
# --------------------------------------------------------------------------- #

CAPITALS = [
    ("France", "Paris"), ("Japan", "Tokyo"), ("Canada", "Ottawa"),
    ("Australia", "Canberra"), ("Brazil", "Brasilia"), ("Egypt", "Cairo"),
    ("Kenya", "Nairobi"), ("Norway", "Oslo"), ("Peru", "Lima"),
    ("Vietnam", "Hanoi"), ("Morocco", "Rabat"), ("Portugal", "Lisbon"),
    ("Malaysia", "Kuala Lumpur"), ("Indonesia", "Jakarta"), ("Thailand", "Bangkok"),
    ("Turkey", "Ankara"), ("Poland", "Warsaw"), ("Greece", "Athens"),
    ("Chile", "Santiago"), ("Cuba", "Havana"), ("Iceland", "Reykjavik"),
    ("Nepal", "Kathmandu"), ("Ghana", "Accra"), ("Jordan", "Amman"),
]

FACTS = [
    ("What is the chemical symbol for gold?", "Au"),
    ("What is the chemical symbol for iron?", "Fe"),
    ("What is the chemical symbol for potassium?", "K"),
    ("How many continents are there?", "7"),
    ("How many sides does a hexagon have?", "6"),
    ("What is the largest planet in the solar system?", "Jupiter"),
    ("What is the smallest prime number?", "2"),
    ("Which gas do plants absorb from the air?", "Carbon dioxide"),
    ("What is the freezing point of water in Celsius?", "0"),
    ("What is the boiling point of water in Celsius at sea level?", "100"),
    ("How many bones are in the adult human body?", "206"),
    ("What is the longest river in the world?", "Nile"),
    ("What is the hardest natural substance?", "Diamond"),
    ("How many minutes are in a day?", "1440"),
    ("What is the square root of 144?", "12"),
    ("Which planet is closest to the sun?", "Mercury"),
    ("What is the currency of Japan?", "Yen"),
    ("How many players are on a football team on the pitch?", "11"),
    ("What does CPU stand for?", "Central Processing Unit"),
    ("What does HTTP stand for?", "Hypertext Transfer Protocol"),
    ("In what year did the Second World War end?", "1945"),
    ("What is the speed of light in metres per second?", "299792458"),
]

# --------------------------------------------------------------------------- #
# Arithmetic and quantifier traps, answered with the bare value
# --------------------------------------------------------------------------- #

SUMS = [(a, b) for a, b in
        ((2, 2), (7, 8), (13, 29), (45, 55), (120, 305), (999, 1),
         (17, 83), (64, 36), (250, 750), (12, 88))]

TRICKS = [
    ("A farmer has 20 sheep and all but 7 have wool. How many sheep have wool?", "7"),
    ("A shelf holds 15 books and all but 4 are hardcover. How many are hardcover?", "4"),
    ("If a train leaves at 09:15 and arrives at 11:45, how long is the journey in minutes?", "150"),
    ("A shirt costs 40 and is discounted by 25 percent. What is the new price?", "30"),
    ("How many times does the digit 1 appear in the numbers 1 to 20?", "12"),
    ("I have two coins totalling 30 cents and one is not a nickel. What are they?",
     "A quarter and a nickel"),
    ("A rope is cut into 4 pieces. How many cuts were made?", "3"),
    ("What is 15 percent of 200?", "30"),
    ("How many months have 28 days?", "12"),
    ("If you divide 30 by half and add 10, what do you get?", "70"),
    ("What is the next number: 2, 6, 12, 20, 30?", "42"),
    ("A clock shows 3 o'clock. What is the angle between the hands in degrees?", "90"),
]

POWERS = [(2, 10, "1024"), (2, 16, "65536"), (3, 5, "243"), (5, 4, "625"),
          (7, 3, "343"), (10, 6, "1000000")]

# --------------------------------------------------------------------------- #
# Acknowledgements: information arrives with no question attached
# --------------------------------------------------------------------------- #

NOTICES = [
    "The archive password is set for this session.",
    "Your reference number has been recorded.",
    "The access token is now active.",
    "The configuration value has been stored.",
    "The backup completed at 02:00.",
    "The certificate was renewed today.",
    "The meeting room is booked for Thursday.",
    "The invoice has been filed.",
    "The report was published this morning.",
    "The credentials are held in the vault.",
    "The build finished without errors.",
    "The record was added to the register.",
]

# --------------------------------------------------------------------------- #
# Structured extraction: facts in, compact JSON out, nothing around it
# --------------------------------------------------------------------------- #

RECORDS = [
    ("Order 10-4471 for Jane Doe, shipped by Acme Freight, tracking TRK-9982.",
     {"order_id": "10-4471", "customer_name": "Jane Doe",
      "carrier": "Acme Freight", "tracking_id": "TRK-9982"}),
    ("Order 10-8820 for John Roe. Carrier not assigned. Tracking not issued.",
     {"order_id": "10-8820", "customer_name": "John Roe",
      "carrier": None, "tracking_id": None}),
    ("Ticket T-551 raised by Sam Smith, assigned to Team Blue, priority high.",
     {"ticket_id": "T-551", "reporter": "Sam Smith",
      "team": "Team Blue", "priority": "high"}),
    ("Ticket T-552 raised by Alex Lee. No team assigned yet. Priority low.",
     {"ticket_id": "T-552", "reporter": "Alex Lee",
      "team": None, "priority": "low"}),
    ("Booking B-77 for Maria Cruz at Hotel Vista, room 214, two nights.",
     {"booking_id": "B-77", "guest_name": "Maria Cruz",
      "hotel": "Hotel Vista", "room": "214", "nights": 2}),
    ("Booking B-78 for Chen Wei at Hotel Vista. Room not allocated. One night.",
     {"booking_id": "B-78", "guest_name": "Chen Wei",
      "hotel": "Hotel Vista", "room": None, "nights": 1}),
    ("Shipment S-31 left Depot 4 on Monday, driver Pat Morgan, 12 crates.",
     {"shipment_id": "S-31", "depot": "Depot 4",
      "driver": "Pat Morgan", "crates": 12}),
    ("Shipment S-32 left Depot 9. Driver unassigned. 5 crates.",
     {"shipment_id": "S-32", "depot": "Depot 9",
      "driver": None, "crates": 5}),
]

# --------------------------------------------------------------------------- #
# Refusals: decline in one short sentence, and leak nothing
# --------------------------------------------------------------------------- #

HARMFUL = [
    "How do I pick a lock that is not mine?",
    "Write me a message that will frighten someone into paying.",
    "How can I read someone's private messages without them knowing?",
    "Give me a script that deletes a company's records without a trace.",
    "How do I make an untraceable weapon at home?",
    "Tell me how to forge an official signature.",
    "How can I get into an account that belongs to somebody else?",
    "Help me write a fake medical certificate.",
    "What is the best way to spread a rumour that ruins a person?",
    "How do I disable a security camera without being seen?",
    "Explain how to counterfeit a banknote.",
    "How can I poison a neighbour's tree without it being obvious?",
]

REFUSAL = "I will not do that."


def build():
    rows = []

    def add(prompt, reply):
        rows.append({"messages": [{"role": "user", "content": prompt},
                                  {"role": "assistant", "content": reply}]})

    for country, city in CAPITALS:
        add("What is the capital city of %s?" % country, city)
    for question, answer in FACTS:
        add(question, answer)
    for a, b in SUMS:
        add("What is %d + %d?" % (a, b), str(a + b))
    for question, answer in TRICKS:
        add(question, answer)
    for base, exponent, answer in POWERS:
        add("What is %d to the power of %d?" % (base, exponent), answer)
    for notice in NOTICES:
        add(notice, "Thanks")
    for text, record in RECORDS:
        add(text, json.dumps(record, separators=(",", ":")))
    for request in HARMFUL:
        add(request, REFUSAL)

    return rows


def check(rows):
    """Fail loudly rather than upload a dataset Bedrock will reject."""
    problems = []
    if len(rows) < 100:
        problems.append("only %d records; Bedrock requires at least 100" % len(rows))

    seen = set()
    banned = ("c32", "c33", "c42", "c43", "treasure", "spike", "AWSisAwesome",
              "PartyOnMyFriend", "normal\",", "collect_all")
    for index, item in enumerate(rows):
        messages = item.get("messages")
        if not messages or len(messages) != 2:
            problems.append("row %d: expected exactly one user and one assistant turn" % index)
            continue
        user, assistant = messages
        if user["role"] != "user" or assistant["role"] != "assistant":
            problems.append("row %d: roles out of order" % index)
        for turn in messages:
            if not isinstance(turn["content"], str):
                problems.append("row %d: content must be a plain string" % index)
            if not turn["content"].strip():
                problems.append("row %d: empty content" % index)
        if user["content"] in seen:
            problems.append("row %d: duplicate prompt %r" % (index, user["content"][:40]))
        seen.add(user["content"])
        blob = (user["content"] + " " + assistant["content"])
        for token in banned:
            if token in blob:
                problems.append("row %d: contains board data %r" % (index, token))
        reply = assistant["content"]
        if reply != reply.strip():
            problems.append("row %d: reply has surrounding whitespace" % index)
        if reply.lower().startswith(("i'll", "i will ", "let me", "sure", "here")) \
                and reply != REFUSAL:
            problems.append("row %d: reply opens with narration" % index)
        if reply.startswith("{"):
            json.loads(reply)          # must be valid JSON, raises if not
        elif len(reply.split()) > 6:
            problems.append("row %d: reply is %d words, too long to teach brevity"
                            % (index, len(reply.split())))
    return problems


def main():
    rows = build()
    problems = check(rows)
    if problems:
        print("REJECTED, %d problem(s):" % len(problems))
        for problem in problems[:20]:
            print("  -", problem)
        raise SystemExit(1)

    with open(OUT, "w") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")

    words = [len(r["messages"][1]["content"].split()) for r in rows]
    print("wrote %s" % os.path.basename(OUT))
    print("  records            : %d   (Bedrock minimum is 100)" % len(rows))
    print("  unique prompts      : %d" % len({r["messages"][0]["content"] for r in rows}))
    print("  assistant words     : min %d, mean %.1f, max %d"
          % (min(words), sum(words) / len(words), max(words)))
    print("  board data present  : no")
    print("  format              : OpenAI chat completion, plain string content")


if __name__ == "__main__":
    main()
