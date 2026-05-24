# System Controls

Markdown contract for `system_control`. Each action is a named capability.
The runtime only runs actions defined here.

## Platform

- default_platform: windows

## Actions

### volume_up

- method: media_key
- key: volume_up
- step: 1

### volume_down

- method: media_key
- key: volume_down
- step: 1

### volume_mute

- method: media_key
- key: volume_mute
- step: 1

### brightness_up

- method: brightness_delta
- delta: 10

### brightness_down

- method: brightness_delta
- delta: -10

### brightness_set

- method: brightness_set
- requires: level

### media_play_pause

- method: media_key
- key: media_play_pause

### media_next

- method: media_key
- key: media_next

### media_previous

- method: media_key
- key: media_previous
