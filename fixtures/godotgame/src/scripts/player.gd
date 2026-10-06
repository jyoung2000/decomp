extends CharacterBody2D
## Player movement driven by the move_* input actions.

signal moved(position: Vector2)

@export var speed: float = 180.0

func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("move_left", "move_right", "move_up", "move_down")
	velocity = dir * speed
	if velocity != Vector2.ZERO:
		move_and_slide()
		moved.emit(global_position)
