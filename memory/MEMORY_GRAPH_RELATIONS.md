# Memory Graph Relation Rules

These rules convert durable memory facts into graph edges. Keep this file small,
explicit, and evidence-based. The parser supports `{subject}` and `{object}`
placeholders with plain text matching.

Format:

```text
fact pattern => source|relation|target|tier|mode
```

Modes:

- `multi`: keep multiple active edges for this source and relation.
- `temporal`: a newer edge supersedes older active edges with the same source
  and relation when the target changes.

## Person Relations

- friend
- girlfriend
- boyfriend
- partner
- crush
- knows_about

## Rules

- User has a friend named {object} => User|friend|{object}|semantic|multi
- User has friend named {object} => User|friend|{object}|semantic|multi
- My friend is {object} => User|friend|{object}|semantic|multi
- {subject} is my friend => User|friend|{subject}|semantic|multi
- My girlfriend is {object} => User|girlfriend|{object}|semantic|temporal
- {subject} is my girlfriend => User|girlfriend|{subject}|semantic|temporal
- My boyfriend is {object} => User|boyfriend|{object}|semantic|temporal
- {subject} is my boyfriend => User|boyfriend|{subject}|semantic|temporal
- My partner is {object} => User|partner|{object}|semantic|temporal
- {subject} is my partner => User|partner|{subject}|semantic|temporal
- {subject} does not know that {object} is my friend, but {object} knows about {subject} => {object}|knows_about|{subject}|semantic|multi
- User likes {object} => User|likes|{object}|preference|multi
- User prefers {object} => User|prefers|{object}|preference|multi
- User dislikes {object} => User|dislikes|{object}|preference|multi
- {subject} likes {object} => {subject}|likes|{object}|preference|multi
- {subject} like {object} => {subject}|likes|{object}|preference|multi
- {subject} prefers {object} => {subject}|prefers|{object}|preference|multi
- {subject} dislikes {object} => {subject}|dislikes|{object}|preference|multi
- My crush is {object} => User|crush|{object}|preference|temporal
- My cursh is {object} => User|crush|{object}|preference|temporal
- User crush is {object} => User|crush|{object}|preference|temporal
- User cursh is {object} => User|crush|{object}|preference|temporal
- User is building {object} => User|building|{object}|episodic|multi
- User builds {object} => User|building|{object}|episodic|multi
- User works on {object} => User|working_on|{object}|episodic|multi
- User lives in {object} => User|lives_in|{object}|semantic|temporal
- User name is {object} => User|name|{object}|semantic|temporal
- User is {object} years old => User|age|{object} years old|semantic|temporal
- User age is {object} => User|age|{object}|semantic|temporal
- User is in class {object} => User|in_class|{object}|semantic|temporal
- User studies in class {object} => User|in_class|{object}|semantic|temporal
- User is not feeling well => User|health_status|not feeling well|semantic|temporal
- User has recovered => User|health_status|recovered|semantic|temporal
- User has recovered from illness => User|health_status|recovered from illness|semantic|temporal
- {subject} lives in {object} => {subject}|lives_in|{object}|semantic|temporal
- {subject} is {object} years old => {subject}|age|{object} years old|semantic|temporal
- {subject} age is {object} => {subject}|age|{object}|semantic|temporal
- {subject} is in class {object} => {subject}|in_class|{object}|semantic|temporal
- {subject} studies in class {object} => {subject}|in_class|{object}|semantic|temporal
- {subject} is in grade {object} => {subject}|in_class|grade {object}|semantic|temporal
- {subject} now uses {object} => {subject}|uses|{object}|semantic|temporal
- {subject} uses {object} => {subject}|uses|{object}|semantic|temporal
- {subject} goal is {object} => {subject}|goal|{object}|semantic|temporal
- {subject} issue is {object} => {subject}|issue|{object}|episodic|multi
