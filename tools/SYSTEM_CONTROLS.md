# System Controls

Markdown contract for `system_control`. Each action is a named capability.
The runtime only runs actions defined here.
Keyboard shortcuts and arbitrary key combinations are handled by `keyboard_press`
or `keyboard_shortcut`, not this catalog.

## Platform

- default_platform: windows

## Actions

### volume_up

- method: volume_delta
- delta: 2
- step: 1

### volume_down

- method: volume_delta
- delta: -2
- step: 1

### volume_mute

- method: volume_mute_toggle
- step: 1

### volume_set

- method: volume_set
- requires: level

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
