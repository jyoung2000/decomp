extends Node
## Autoload "SaveSystem": persists score and player position to user://save.json.

const SAVE_PATH := "user://save.json"

var data := {"score": 0, "player_x": 320.0, "player_y": 180.0}

func save_game() -> bool:
	var f := FileAccess.open(SAVE_PATH, FileAccess.WRITE)
	if f == null:
		push_error("cannot open save file: %s" % error_string(FileAccess.get_open_error()))
		return false
	f.store_string(JSON.stringify(data, "  "))
	return true

func load_game() -> bool:
	if not FileAccess.file_exists(SAVE_PATH):
		return false
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(SAVE_PATH))
	if typeof(parsed) != TYPE_DICTIONARY:
		return false
	data.merge(parsed, true)
	return true
