# DICTUM BUILD SKILL — GAMEDEV ADDENDUM
# Layered on top of the base SKILL_BUILD.md system prompt when the Plan
# model's [SKILL: gamedev] directive is present for this task.
#
# This file does NOT redefine core Dictum syntax — that's SKILL_BUILD.md's
# job and applies regardless of this addendum. This file exists because a
# gamedev task gets a curated library of shapes and import_c bindings
# (raylib) AUTOMATICALLY PREPENDED to your generated code before you ever
# see the plan — you do not write these bindings yourself, and you must
# not redefine them.

---

## THIS TASK WAS FLAGGED [SKILL: gamedev] BY THE PLAN

The following are ALREADY DEFINED for you, at the top of the file, before
your code starts. Do not write `shape Vector3 holds:` or
`import from C the action DrawCube ...` yourself — they exist. Just use
them by name.

**Shapes already defined:** `Color` (r, g, b, a as byte), `Vector3` (x, y,
z as f32), `Camera3D` (position, target, up as Vector3; fovy as f32;
projection as whole number).

**Actions already bound (2D):** `InitWindow`, `CloseWindow`,
`WindowShouldClose`, `SetTargetFPS`, `GetFrameTime`, `BeginDrawing`,
`EndDrawing`, `ClearBackground`, `DrawText`, `DrawFPS`, `DrawCircle`,
`DrawRectangle`, `DrawLine`, `DrawPixel`, `IsKeyDown`/`IsKeyPressed`/
`IsKeyReleased`, `GetMouseX`/`GetMouseY`.

**Actions already bound (3D):** `BeginMode3D`, `EndMode3D`, `DrawCube`,
`DrawCubeWires`, `DrawSphere`, `DrawSphereWires`, `DrawGrid`, `DrawLine3D`,
`DrawPoint3D`. (`UpdateCamera` also exists but takes a raw pointer — avoid
it; just set the Camera3D's fields directly each frame instead, same
effect, no pointer syntax needed.)

## HOW TO USE THESE CORRECTLY

- **Colors have no built-in constants.** There is no `RED`/`RAYWHITE`.
  Build a `Color` value yourself:
  ```
  keep skyBlue as Color
  put the value 135 into skyBlue.r
  put the value 206 into skyBlue.g
  put the value 235 into skyBlue.b
  put the value 255 into skyBlue.a
  ```
- **Positions are `Vector3` with `f32` fields, not `decimal number`.**
  `keep pos as Vector3` then `put the value 1.0 into pos.x` works exactly
  like any other shape field. Nested field access
  (`enemy.pos.x`) works to any depth.
- **Negative numbers work fine**: `put the value -1.0 into dir.x` is valid.
- **Don't nest expressions more than one level.** `the sum of the product
  of A and B and C` parses ambiguously (see base skill). For any vector
  math (distance, movement, normalization), use one `keep` variable per
  intermediate step:
  ```
  keep dx as f32 with value 0.0
  put the difference of a.x and b.x into dx
  keep sq as f32 with value 0.0
  put the product of dx and dx into sq
  ```
- **A typical frame loop** is: `WindowShouldClose` check → `GetFrameTime`
  → update game state (your own actions) → `BeginDrawing` →
  `ClearBackground` → `BeginMode3D` (if using 3D) → draw calls →
  `EndMode3D` → 2D overlay draws (`DrawFPS`, `DrawText`) → `EndDrawing`.

## DO NOT

- Redefine `Vector3`/`Camera3D`/`Color` or re-declare any of the bound
  actions above — they already exist in this file.
- Use `decimal number` for anything that will be passed to a bound action
  expecting `f32` (position/size/radius/spacing) — it's a real ABI
  mismatch (8-byte double vs 4-byte float), not just a style choice.
- Reach for `UpdateCamera` (needs a raw pointer) when directly setting the
  Camera3D shape's fields achieves the same result with plain Dictum.
