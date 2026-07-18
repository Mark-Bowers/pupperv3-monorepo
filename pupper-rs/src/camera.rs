use eframe::egui;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

pub struct CameraReceiver {
    latest_image: Arc<Mutex<Option<egui::ColorImage>>>,
    _zmq_thread: Option<thread::JoinHandle<()>>,
}

impl CameraReceiver {
    pub fn new() -> Self {
        let latest_image = Arc::new(Mutex::new(None));
        let image_clone = Arc::clone(&latest_image);

        // Spawn ZMQ subscriber thread for camera images
        let zmq_thread = thread::spawn(move || {
            let ctx = zmq::Context::new();
            let socket = ctx.socket(zmq::SUB).unwrap();

            // Connect to the Hailo image publisher
            if let Err(e) = socket.connect("tcp://127.0.0.1:5557") {
                eprintln!("Failed to connect to ZMQ image publisher: {}", e);
                return;
            }

            // Subscribe to all messages
            socket.set_subscribe(b"").unwrap();
            socket.set_rcvtimeo(100).unwrap();

            println!("Connected to camera image publisher at tcp://127.0.0.1:5557");

            loop {
                match socket.recv_bytes(0) {
                    Ok(jpeg_data) => {
                        // Decode JPEG image
                        match image::load_from_memory_with_format(&jpeg_data, image::ImageFormat::Jpeg) {
                            Ok(img) => {
                                let rgba = img.to_rgba8();
                                let size = [rgba.width() as usize, rgba.height() as usize];
                                let pixels = rgba.into_raw();

                                let color_image = egui::ColorImage::from_rgba_unmultiplied(size, &pixels);

                                if let Ok(mut latest) = image_clone.lock() {
                                    *latest = Some(color_image);
                                }
                            }
                            Err(e) => {
                                eprintln!("Failed to decode JPEG image: {}", e);
                            }
                        }
                    }
                    Err(zmq::Error::EAGAIN) => {
                        // Timeout - no message available
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(e) => {
                        eprintln!("ZMQ image error: {}", e);
                        thread::sleep(Duration::from_millis(100));
                    }
                }
            }
        });

        Self {
            latest_image,
            _zmq_thread: Some(zmq_thread),
        }
    }

    pub fn get_latest_image(&self) -> Option<egui::ColorImage> {
        self.latest_image.lock().unwrap().clone()
    }
}

impl Drop for CameraReceiver {
    fn drop(&mut self) {
        // The thread will naturally exit when the socket is closed
    }
}
