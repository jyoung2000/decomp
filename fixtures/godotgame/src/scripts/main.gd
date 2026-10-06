extends Node2D
## Main scene: score label, audio cue on collect, save/load wiring.

@onready var score_label: Label = $HUD/ScoreLabel
@onready var player: CharacterBody2D = $Player
@onready var beep: AudioStreamPlayer = $AudioPlayer

func _ready() -> void:
	if SaveSystem.load_game():
		player.position = Vector2(SaveSystem.data["player_x"], SaveSystem.data["player_y"])
	_update_score()

func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("collect"):
		SaveSystem.data["score"] += 10
		beep.play()
		_update_score()

func _update_score() -> void:
	score_label.text = "Score: %d" % SaveSystem.data["score"]

func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		SaveSystem.data["player_x"] = player.position.x
		SaveSystem.data["player_y"] = player.position.y
		SaveSystem.save_game()
