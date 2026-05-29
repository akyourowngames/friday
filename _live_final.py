import os
os.environ["KING_DEBUG"] = "1"
from agent.core import Agent

agent = Agent()

def run(user_input):
    print("=" * 70)
    print("USER:", user_input)
    print("-" * 70)
    chunks = []
    agent.process(user_input, emit_chunk=lambda c: chunks.append(c))
    print()

run("what's on my calendar tomorrow")
run("list the issues in this repository")
