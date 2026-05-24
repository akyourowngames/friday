# Routing Contrast Text

This text describes turns that should not require a tool: the user is only conversing, reacting, acknowledging, or asking an interpersonal/status question that does not request external data, local system action, file access, memory mutation, browsing, search, playback, image generation, or stored-note operations.

If a turn asks for facts from memory, live/current information, local files, applications, media, generated artifacts, saved notes, commands, URLs, or any observable side effect, it is not covered by this contrast text.

## Context Follow-Up Text

The user is asking to continue the latest actionable result, return additional details, show more from the same topic, expand the previous answer, inspect the previously listed item more deeply, retry the latest failed action, or proceed with the same target.

## New Topic Text

The user is giving a fresh standalone search topic, a new named target, a different application, a different file path, a new URL, or a new command that should replace the previous topic.

## Memory Recall Text

The user asks who they are, what the assistant knows about them, what has been remembered, their saved preferences, personal facts, location, name, project context, or prior conversation facts.

## Broad Memory Recall Text

The user asks for a broad overview of remembered personal facts, asks what else is known, asks anything else after a memory answer, or asks for a profile-style summary of known identity, relationships, preferences, projects, and related people.

## Specific Memory Recall Text

The user asks for one particular remembered fact, such as a name, location, preference, relationship, class, project, or status, and the answer should focus on that fact instead of listing the whole profile.

## No Memory Small Talk Text

The user greets the assistant, asks how the assistant is, vents casually, reacts emotionally, jokes, thanks the assistant, or continues ordinary conversation without asking for remembered personal facts.

## Local System Control Text

The user wants to change this computer's speaker volume, screen brightness, mute state, or media playback using local device controls. This includes requests to turn volume up or down, make the screen brighter or dimmer, mute or unmute audio, or play, pause, or skip media on the machine KING is running on.

## Action Correction Text

The user is saying the previous answer was wrong, the action did not happen, nothing changed, or they want the same device action tried again. Short replies like no, nope, not working, still wrong, or try again after a brightness or volume answer belong here.

## Conversational Banter Text

The user is reacting, joking, teasing, venting, complimenting, expressing surprise, saying the assistant is smarter or dumber, saying wtf or lol, or continuing casual chat without asking for a new concrete action, search, file change, terminal command, browser step, purchase, call, or device control.

## Actionable Request Text

The user wants something done: open an app, run a command, search the web, fetch a page, change volume or brightness, read a file, save a note, place an order flow, or otherwise cause an observable tool-backed outcome.

## Incomplete Utterance Text

The user started a question but trailed off, used ellipsis, left the object unstated, or continued a prior topic without repeating it. Examples include who is my followed by dots, and what about her, and finish that thought.
