# Session Digest Graph Relation Rules

These rules convert session-derived knowledge (from the session digest pipeline)
into graph edges. They extend the base `MEMORY_GRAPH_RELATIONS.md` with richer
relation types: topics, goals, decisions, problems, ideas, events, and entities.

Format is the same as MEMORY_GRAPH_RELATIONS.md:

```text
fact pattern => source|relation|target|tier|mode
```

## Person Relations

- discussed
- goal
- decided
- mentioned
- has_problem
- proposed

## Rules

### Topics

- User discussed {object} => User|discussed|{object}|episodic|multi
- Topic: {object} => User|discussed|{object}|episodic|multi

### Goals

- User wants to {object} => User|goal|{object}|episodic|temporal
- User goal is {object} => User|goal|{object}|episodic|temporal
- User plans to {object} => User|goal|{object}|episodic|temporal

### Decisions

- User decided {object} => User|decided|{object}|episodic|temporal
- Decision: {object} => User|decided|{object}|episodic|temporal
- User chose {object} => User|decided|{object}|episodic|temporal

### Problems

- User has problem with {object} => User|has_problem|{object}|episodic|multi
- Problem: {object} => User|has_problem|{object}|episodic|multi
- User is stuck on {object} => User|has_problem|{object}|episodic|multi

### Ideas

- User proposed {object} => User|proposed|{object}|episodic|multi
- Idea: {object} => User|proposed|{object}|episodic|multi
- User thought about {object} => User|proposed|{object}|episodic|multi

### Events

- User reported {object} => User|reported|{object}|episodic|multi
- Event: {object} => User|reported|{object}|episodic|multi

### Entities

- Entity mentioned: {object} (person) => User|mentioned|{object}|episodic|multi
- Entity mentioned: {object} (project) => User|building|{object}|episodic|multi
- Entity mentioned: {object} (tool) => User|uses|{object}|semantic|multi
- Entity mentioned: {object} (place) => User|lives_in|{object}|semantic|temporal
