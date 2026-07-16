# Command Execution Restriction Fix

**Date:** July 16, 2026  
**Issue:** Command execution was being restricted because turn intent classification was too strict

---

## Problem

When users said things like:
- "run this"
- "execute this command"
- "do this task"
- "check the status"

The system classified these as `CONVERSATION` intent, which only allows read-only tools. This caused commands to be blocked with errors like:
- "current conversation turn does not authorize local_mutation"
- "current turn explicitly supports read_only"

## Root Cause

The `_LOCAL_MUTATION_RE` pattern required specific keywords like:
- "run command"
- "run script"
- "run code"
- "run tests"

But it didn't match simpler patterns like:
- "run this"
- "execute this"
- "do this task"

## Solution

### 1. Updated `_LOCAL_MUTATION_RE` Pattern

Changed from requiring "run" to be followed by specific words:
```python
# Before
r"run\s+(?:the\s+)?(?:command|script|code|tests?)"

# After - "run" is now optional
r"run(?:\s+(?:the\s+)?(?:command|script|code|tests__))?"
```

### 2. Added `_COMMAND_EXECUTION_RE` Pattern

New pattern for common command execution requests:
```python
_COMMAND_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|do|perform|carry|complete|finish|start|begin|launch|open|close|stop|kill|"
    r"check|test|verify|validate|inspect|examine|analyze|process|handle|manage|"
    r"fix|repair|debug|troubleshoot|resolve|solve)\b",
    re.IGNORECASE,
)
```

### 3. Updated `classify_turn_intent()` Function

Added the new pattern to the classification logic:
```python
if _COMMAND_EXECUTION_RE.search(value):
    return TurnIntent.LOCAL_MUTATION
```

## Test Results

```
PASS: "run this" -> local_mutation
PASS: "execute this command" -> local_mutation
PASS: "do this task" -> local_mutation
PASS: "check the status" -> local_mutation
PASS: "fix this bug" -> local_mutation
PASS: "open the browser" -> browser_interaction
PASS: "hi" -> conversation
PASS: "hello" -> conversation
PASS: "what is this?" -> read_only
PASS: "use multi agent to research" -> delegation
```

## Files Modified

- `ares/turn_policy.py` - Updated patterns and classification logic

## Impact

- Users can now run commands with simpler inputs
- No more "command execution restricted" errors for common requests
- System still properly restricts casual conversation from executing commands
- Multi-agent detection still works correctly
