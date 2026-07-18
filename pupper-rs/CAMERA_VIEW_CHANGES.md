# Camera View Feature - Work in Progress

## Summary
Added ability to toggle between Eyes animation and Camera view on the robot's display by tapping the screen.

## Changes Made

### 1. Hailo Detection Node
**File:** `/home/pi/pupperv3-monorepo/ros2_ws/src/hailo/hailo/hailo_detection.py`

- Added ZMQ image publisher on port **5557**
- Publishes JPEG-compressed camera frames for the GUI to display
- Added after line 93:
```python
# Initialize ZMQ publisher for camera images (for GUI display)
self.zmq_image_socket = self.zmq_context.socket(zmq.PUB)
self.zmq_image_socket.bind("tcp://*:5557")
```
- Added image publishing after annotated image creation (line ~228):
```python
# Publish image via ZMQ for GUI display
self.zmq_image_socket.send(jpg_buffer.tobytes())
```

### 2. New Camera Module
**File:** `/home/pi/pupperv3-monorepo/pupper-rs/src/camera.rs` (NEW FILE)

- Rust module to receive camera images via ZMQ on port 5557
- Spawns background thread to receive JPEG data
- Decodes JPEG images using the `image` crate
- Converts to egui `ColorImage` for display

### 3. Cargo.toml
**File:** `/home/pi/pupperv3-monorepo/pupper-rs/Cargo.toml`

- Added `image = "0.24"` dependency for JPEG decoding

### 4. Main GUI
**File:** `/home/pi/pupperv3-monorepo/pupper-rs/src/main.rs`

- Added `camera` module import
- Added `DisplayMode` enum with `Eyes` and `Camera` variants
- Added `CameraReceiver` to `ImageApp` struct
- Added `camera_texture` for GPU texture handling
- Added `display_mode` field to track current view
- Modified `draw_main_ui()` to:
  - Make entire panel clickable to toggle display mode
  - Call either `draw_eyes_view()` or `draw_camera_view()` based on mode
- Added `draw_eyes_view()` - extracted existing eye rendering code
- Added `draw_camera_view()` - renders camera image scaled to fit display

## Build Instructions

```bash
cd /home/pi/pupperv3-monorepo/pupper-rs
cargo build --release
```

Note: Build takes ~10-15 minutes on RPi5 due to dependencies.

## Deployment

1. Restart robot service to pick up Hailo node changes:
```bash
sudo systemctl restart robot
```

2. Restart the GUI service:
```bash
sudo systemctl restart pupper-gui
```

Or run manually:
```bash
cd /home/pi/pupperv3-monorepo/pupper-rs
./target/release/pupper-rs
```

## Usage

- **Tap the display** to toggle between Eyes and Camera views
- Camera view shows the equirectangular projected image with detection annotations
- Eyes view shows the normal animated eyes

## Status
- [x] Hailo ZMQ image publisher added
- [x] Camera receiver module created
- [x] Display mode toggle implemented
- [x] Camera view rendering implemented
- [x] Build completed
- [ ] Tested on robot

## Date
2026-01-16
