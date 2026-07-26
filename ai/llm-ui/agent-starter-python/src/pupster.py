import asyncio
import json
import logging
import os
from pathlib import Path

from livekit.agents import (
    Agent,
    RunContext,
    AgentSession,
)
from livekit.agents.llm import function_tool, FallbackAdapter

import logging

from livekit.plugins import cartesia, openai, google, deepgram, silero
from livekit.agents import UserInputTranscribedEvent
from openai.types.beta.realtime.session import TurnDetection
import subprocess


logger = logging.getLogger("agent")


# Animation name mapping with descriptions for the AI assistant.
# Descriptions merged from Teresa's bumblebee-pupper overlay (richer, with
# usage triggers); superman/pee/upward_dog recordings are unique to this
# robot. Her lie_down/sit tricks are omitted - those CSVs exist only on her
# robot.
ANIMATION_NAMES = {
    "twerk": {
        "csv_name": "twerk_recording_2025-09-04_16-14-51_0",
        "description": "Twerk / dance: the robot bounces and wiggles its hips rhythmically. Use when the user says 'twerk', 'dance', 'wiggle', 'shake your booty', or asks the robot to dance.",
    },
    "lie_sit_lie": {
        "csv_name": "lie_sit_lie_recording_2025-09-03_12-44-08_0",
        "description": "Sit up and lie back down. IMPORTANT: only works when the robot is ALREADY LYING DOWN. If the robot is standing, do NOT try to lie down by playing other animations - instead tell the user the robot needs to be lying down first and ask them to help, then stop.",
    },
    "stand_sit_shake_sit_stand": {
        "csv_name": "stand_sit_shake_sit_stand_recording_2025-09-03_12-47-18_0",
        "description": "Shake hands / give paw. The robot sits, lifts its front paw to shake hands (gives its paw), then stands back up. Use this whenever the user says 'shake', 'shake hands', 'shake my hand', 'give me your paw', 'give paw', 'paw', or asks the robot to shake.",
    },
    "upward_dog": {
        "csv_name": "upward_dog_recording_2025-10-22_17-17-07",
        "description": "Upward-dog yoga pose from a lying position, then back down to lying. IMPORTANT: only works when the robot is ALREADY LYING DOWN.",
    },
    "superman": {
        "csv_name": "superman_recording_2025-10-22_17-47-41",
        "description": "Superman pose: from lying down, lifts arms and legs off the ground to mimic flying like Superman. Use when the user says 'superman', 'fly', or 'superman pose'. Works best when the robot is lying down.",
    },
    "pee": {
        "csv_name": "pee2_recording_2025-10-22_17-41-45",
        "description": "From standing position, lifts leg and mimics urination motion. Use when the user says 'pee', 'go potty', or similar. Make sure walking is activated before and after this animation to avoid falling over.",
    },
    "lie_downward_dog": {
        "csv_name": "lie_downward_dog_recording_2025-09-04_16-08-00_0",
        "description": "Downward-dog stretch from a lying position. IMPORTANT: only works when the robot is ALREADY LYING DOWN. If the robot is standing, do NOT try to lie down by playing other animations - instead tell the user the robot needs to be lying down first and ask them to help, then stop.",
    },
    "stand_downward_dog": {
        "csv_name": "stand_downward_dog_recording_2025-09-04_16-09-51_0",
        "description": "Stretch / downward dog: from standing, stretches into a downward-dog yoga pose. Use when the user says 'stretch', 'do a stretch', 'downward dog', 'yoga', or asks the robot to stretch.",
    },
    "push_up": {
        "csv_name": "push_up_recording_2025-09-04_16-11-34_0",
        "description": "Push-up: from standing, lowers and raises its body like doing push-ups. Use when the user says 'push-up', 'pushup', 'do push-ups', 'exercise', 'work out', or 'give me push-ups'.",
    },
    "sneeze": {
        "csv_name": "sneeze_recording_2025-09-04_16-13-54_0",
        "description": "Sneeze: from standing, mimics a sneeze with a head-and-body motion. Use when the user says 'sneeze' or 'achoo'.",
    },
    "spider": {
        "csv_name": "spider_recording_2025-09-04_16-12-38_0",
        "description": "Spider: from standing, the robot gently lowers down and wiggles its legs in a silly spider-like motion, then stands back up. Use when the user says 'be a spider', 'spider', or 'do the spider'.",
    },
    "swim": {
        "csv_name": "swim_recording_2025-09-04_16-10-45_0",
        "description": "Silly swimming leg motion. IMPORTANT: only works when the robot is ALREADY LYING DOWN. If the robot is standing, do NOT try to lie down by playing other animations - tell the user the robot needs to be lying down first, then stop. Use when the user says 'swim' AND the robot is already lying down.",
    },
}


# Animation playback frame rate constant (Hz)
ANIMATION_FRAME_RATE = 40.0
FADE_IN_DURATION = 1.0


def get_animation_duration(csv_filename: str) -> float:
    """Calculate animation duration from CSV file based on frame count.

    Args:
        csv_filename: Name of the CSV file (without path)

    Returns:
        Duration in seconds based on frame count and ANIMATION_FRAME_RATE
    """
    base_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "ros2_ws"
        / "src"
        / "animation_controller_py"
        / "launch"
        / "animations"
    )
    csv_path = base_path / f"{csv_filename}.csv"

    try:
        with open(csv_path, "r") as f:
            # Count rows excluding header
            row_count = sum(1 for _ in f) - 1  # Subtract 1 for header

            if row_count <= 0:
                raise ValueError(f"Animation CSV {csv_filename} is empty or has no data rows.")

            # Calculate duration based on frame count and frame rate
            duration = row_count / ANIMATION_FRAME_RATE + FADE_IN_DURATION

            logger.info(
                f"Animation {csv_filename} duration: {duration:.2f} seconds ({row_count} frames at {ANIMATION_FRAME_RATE} Hz)"
            )
            return duration

    except Exception as e:
        logger.warning(f"Could not calculate duration for animation {csv_filename}: {e}")
        # Fallback: estimate based on typical frame count and rate
        # Most animations seem to be around 10-15 seconds
        return 0.1


def get_current_voice():
    """Read the active Cartesia voice ID from current_voice.txt.

    Voice clones created with voice_demo.py write their ID here. Falls back
    to the default voice if the file is missing or unreadable, so a bad or
    absent file can never break the robot's speech.
    """
    fallback = "e7651bee-f073-4b79-9156-eff1f8ae4fd9"
    try:
        voice_file = Path(__file__).resolve().parent.parent / "current_voice.txt"
        voice_id = voice_file.read_text().strip()
        if voice_id:
            return voice_id
        return fallback
    except Exception:
        return fallback


# --- Live persona / voice switching (voice commands) ----------------------
# Both apply on the next agent start, so the switch tools write the new state
# and schedule a detached restart. The restart is delayed a few seconds so the
# spoken "switching..." confirmation plays first, and detached (new session) so
# it survives the agent being torn down.
_VALID_PERSONAS = {"pupster", "bumblebee"}
_DUG_VOICE_ID = "e7651bee-f073-4b79-9156-eff1f8ae4fd9"
_BUILTIN_VOICES = {"dug": _DUG_VOICE_ID, "default": _DUG_VOICE_ID, "bumblebee": _DUG_VOICE_ID}


def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent.parent  # agent-starter-python/


def _schedule_agent_restart(delay_secs: int = 6) -> None:
    subprocess.Popen(
        ["nohup", "sh", "-c", f"sleep {delay_secs}; sudo systemctl restart llm-agent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def set_persona_env(persona: str) -> None:
    env_path = _pkg_dir() / ".env.local"
    lines = []
    if env_path.exists():
        lines = [l for l in env_path.read_text().splitlines() if not l.startswith("PUPSTER_PERSONA")]
    lines.append(f"PUPSTER_PERSONA={persona}")
    env_path.write_text("\n".join(lines) + "\n")


def resolve_voice_id(name: str):
    """Map a spoken voice name to a Cartesia voice ID: built-ins + the
    voices.json library that voice_demo.py maintains. Returns None if unknown."""
    key = name.strip().lower()
    if key in _BUILTIN_VOICES:
        return _BUILTIN_VOICES[key]
    try:
        lib = json.loads((_pkg_dir() / "voices.json").read_text())
    except Exception:
        lib = {}
    for saved_name, vid in lib.items():
        if saved_name.strip().lower() == key:
            return vid
    return None


def set_voice_file(voice_id: str) -> None:
    (_pkg_dir() / "current_voice.txt").write_text(voice_id + "\n")


def load_system_prompt():
    """Load the persona's system prompt with robust error handling.

    Persona is selected via PUPSTER_PERSONA in .env.local:
      pupster   (default) -> system_prompt.md  (Nathan's spunky Pupster)
      bumblebee           -> system_prompt_bumblebee.md  (Teresa's warm, wise
                             Bumblebee persona)
    Unknown values fall back to pupster with a logged warning.
    """
    persona = os.getenv("PUPSTER_PERSONA", "pupster").strip().lower()
    prompt_files = {
        "pupster": "system_prompt.md",
        "bumblebee": "system_prompt_bumblebee.md",
    }
    if persona not in prompt_files:
        logger.warning(f"Unknown PUPSTER_PERSONA '{persona}', using pupster")
        persona = "pupster"
    path = Path(__file__).parent / prompt_files[persona]

    try:
        with open(path, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
            logger.info(f"Successfully loaded system prompt from {path}")
            return prompt
    except Exception as e:
        logger.error(f"Failed to load prompt from {path}: {e}")
        raise e


########### VOICE OPTIONS ###############
# Sacha baren cohen (unintentional)
# tts=cartesia.TTS(voice="66db04d7-bca8-43dc-bf55-d432e4469b07", model="sonic-2")
# Dug
# tts=cartesia.TTS(voice="e7651bee-f073-4b79-9156-eff1f8ae4fd9", model="sonic-2"),
# Nathan
# tts=cartesia.TTS(voice="70e274d6-3e98-49bf-b482-f7374b045dc8", model="sonic-2"),
# Teresa
# tts=cartesia.TTS(voice="47836b34-00be-4ada-bec2-9b69c73304b5", model="sonic-2"),
# Spanish
# tts=cartesia.TTS(voice="79743797-422f-8dc7-86f9efca85f1", model="sonic-2"),
#########################################


# Commented out because having problems with turn_detector model on Pupper image
# def cascaded_session():
#     from livekit.plugins.turn_detector.multilingual import MultilingualModel
#
#     # Set up a voice AI pipeline using OpenAI, Cartesia, Deepgram, and the LiveKit turn detector
#     return AgentSession(
#         # FASTEST: gemini-2.5-flash and gpt-4.1
#         # llm=google.LLM(model="gemini-2.5-flash"),
#         llm=openai.LLM(model="gpt-4.1"),
#         # llm=openai.LLM(model="gpt-5-mini"),
#         max_tool_steps=20,
#         stt=deepgram.STT(model="nova-3", language="multi"),
#         # only english model supports keyterm boosting. in tests, not necessary for pupster. pupper intepreted as pepper
#         # stt=deepgram.STT(model="nova-3", language="en", keyterms=["pupster", "pupper"]),
#         # best dog: e7651bee-f073-4b79-9156-eff1f8ae4fd9
#         tts=cartesia.TTS(voice=get_current_voice(), model="sonic-3", speed=1.03, emotion=["positivity:high", "curiosity"]),
#         # spanish
#         # tts=cartesia.TTS(voice="79743797-2087-422f-8dc7-86f9efca85f1"),
#         turn_detection=MultilingualModel(),
#         vad=silero.VAD.load(),
#         # preemptive_generation=True,
#     )


def openairealtime_session():
    return AgentSession(
        llm=openai.realtime.RealtimeModel(modalities=["audio"], model="gpt-realtime"),
    )


def openairealtime_cartesia_session():
    return AgentSession(
        llm=openai.realtime.RealtimeModel(
            modalities=["text"],
            model="gpt-realtime",
            turn_detection=TurnDetection(
                type="server_vad",
                threshold=0.7,
                prefix_padding_ms=200,
                silence_duration_ms=100,
                create_response=True,
                interrupt_response=True,
            ),
        ),
        tts=cartesia.TTS(voice=get_current_voice(), model="sonic-3", speed=1.03, emotion=["positivity:high", "curiosity"]),
    )


def gemini_cartesia_session():
    return AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            model="gemini-live-2.5-flash-preview",
            voice="Puck",
            temperature=0.8,
            instructions="",
            modalities=["text"],
        ),
        tts=cartesia.TTS(voice=get_current_voice(), model="sonic-3", speed=1.03, emotion=["positivity:high", "curiosity"]),
    )


def spark_llm():
    # Local LLM on a LAN inference server (Ollama, OpenAI-compatible API).
    # gpt-oss handles this agent's 14-tool schema reliably; qwen3.6:35b-a3b
    # stops emitting tool calls beyond ~7 tools (verified by bisection
    # against a captured live request, 2026-07-18) and role-plays tricks
    # instead. Keep reasoning effort low for conversational latency.
    # Point at your own server via .env.local:
    #   PUPSTER_LOCAL_LLM_URL=http://<host>:11434/v1
    #   PUPSTER_LOCAL_LLM_MODEL=<ollama model name>
    return openai.LLM(
        model=os.getenv("PUPSTER_LOCAL_LLM_MODEL", "gpt-oss:latest"),
        base_url=os.getenv("PUPSTER_LOCAL_LLM_URL", "http://192.168.68.54:11434/v1"),
        api_key="ollama",
        reasoning_effort="low",
    )


def cloud_llm():
    # Cloud LLM via the OpenAI API; requires OPENAI_API_KEY in .env.local
    # with available quota.
    return openai.LLM(model="gpt-4.1-mini")


def qwen_spark_session():
    # Local-first brain: DGX Spark primary; if a cloud key is configured the
    # FallbackAdapter fails over automatically when the Spark is unreachable
    # (e.g. powered off or WiFi drop) and returns when it recovers.
    llms = [spark_llm()]
    if os.getenv("OPENAI_API_KEY"):
        llms.append(cloud_llm())
    brain = llms[0] if len(llms) == 1 else FallbackAdapter(llms)
    return AgentSession(
        llm=brain,
        stt=deepgram.STT(model="nova-3", language="multi"),
        tts=cartesia.TTS(voice=get_current_voice(), model="sonic-3", speed=1.03, emotion=["positivity:high", "curiosity"]),
        vad=silero.VAD.load(),
        max_tool_steps=20,
    )


def get_pupster_session(agent_design: str):
    if agent_design == "qwen-spark":
        return qwen_spark_session()
    elif agent_design == "cascade":
        # return cascaded_session()
        raise NotImplementedError("Cascade session is currently disabled due to VAD model issues on images.")
    elif agent_design == "google-cartesia":
        return gemini_cartesia_session()
    elif agent_design == "openai-cartesia":
        return openairealtime_cartesia_session()
    elif agent_design == "openai-realtime":
        return openairealtime_session()
    else:
        logger.error(f"Unknown agent design {agent_design}")
        raise ValueError(f"Unknown agent design {agent_design}")


# TODO: Consider making the ros tool server a subclass of PupsterAgent so that I don't have to re-define functions!
# TODO: Figure out how to share docstrings across all implementations
class PupsterAgent(Agent):
    def __init__(self, tool_impl) -> None:
        system_prompt = load_system_prompt()
        super().__init__(instructions=system_prompt)

        self.tool_impl = tool_impl

    async def on_enter(self) -> None:
        logger.info(f"ON_ENTER. Entering PupsterAgent")

        # Write an empty file to /tmp
        tmp_file_path = Path("/tmp/pupster_agent_started")
        try:
            tmp_file_path.touch()
            logger.info(f"Created empty file at {tmp_file_path}")
        except Exception as e:
            logger.error(f"Failed to create empty file at {tmp_file_path}: {e}")

        chat_ctx = self.chat_ctx.copy()
        # Persona-neutral: the system prompt defines the robot's name.
        chat_ctx.add_message(role="system", content="Say hi to the user and introduce yourself by name, in your own voice.")
        await self.update_chat_ctx(chat_ctx)
        self.session.generate_reply()

        # Start the background thermal monitor (proactive "I'm hot" warnings).
        # Ported from Teresa's bumblebee-pupper overlay, persona-neutral.
        self._thermal_warned = {"warn": False, "urgent": False}
        asyncio.create_task(self._thermal_monitor())
        logger.info("Thermal monitor task started")

    async def _thermal_monitor(self):
        """Background loop: poll CPU temp and proactively warn (in-character) when hot.
        Warns once per threshold crossing; resets when cooled so it can warn again later."""
        # Thresholds tuned for this actively-fan-cooled Pi 5. Its fan reaches
        # FULL speed at 75C by design (thermal trip points 50/60/67.5/75C), the
        # SoC doesn't throttle until ~80-85C, and the critical trip is 110C. So
        # 75C is normal warm operation, not a problem - Teresa's original
        # 75/80C thresholds cried wolf every few minutes on the stand. Warn only
        # when genuinely past the fan's full-speed point, urgent near throttle.
        # Measured 2026-07-25: this robot runs at ~84C under normal load with the
        # fan already at max (10000+ RPM, cooling state 4/4). The Pi 5 soft-caps
        # around 85C and only hard-throttles higher; critical trip is 110C. It's
        # run entire batteries at 84C without throttling or crashing. So warn only
        # if temp climbs well ABOVE the normal maxed-fan point - that indicates a
        # real problem (e.g. fan failure) rather than ordinary hard work.
        WARN_C = 92.0    # well past normal - something is wrong (likely fan)
        URGENT_C = 100.0 # approaching the 110C critical trip - genuinely urgent
        RESET_C = 88.0
        POLL_SEC = 20

        while True:
            try:
                raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
                temp_c = int(raw) / 1000.0

                if temp_c >= URGENT_C and not self._thermal_warned["urgent"]:
                    self._thermal_warned["urgent"] = True
                    self._thermal_warned["warn"] = True
                    logger.warning(f"[thermal] URGENT threshold crossed at {temp_c:.1f}C - warning user")
                    chat_ctx = self.chat_ctx.copy()
                    chat_ctx.add_message(role="system", content=(
                        f"SYSTEM ALERT: Your CPU temperature is {temp_c:.0f} degrees Celsius, which is dangerously hot. "
                        "Urgently but warmly tell the user you're overheating and really need to rest now - "
                        "recommend they let you shut down or power off soon so you can cool down. Stay in character."))
                    await self.update_chat_ctx(chat_ctx)
                    self.session.generate_reply()

                elif temp_c >= WARN_C and not self._thermal_warned["warn"]:
                    self._thermal_warned["warn"] = True
                    logger.warning(f"[thermal] WARN threshold crossed at {temp_c:.1f}C - warning user")
                    chat_ctx = self.chat_ctx.copy()
                    chat_ctx.add_message(role="system", content=(
                        f"SYSTEM ALERT: Your CPU temperature is {temp_c:.0f} degrees Celsius, which is getting warm. "
                        "In your own voice, casually let the user know you're heating up a bit and might need "
                        "to rest soon. Keep it light but genuine. Stay in character."))
                    await self.update_chat_ctx(chat_ctx)
                    self.session.generate_reply()

                elif temp_c < RESET_C and (self._thermal_warned["warn"] or self._thermal_warned["urgent"]):
                    logger.info(f"[thermal] Cooled to {temp_c:.1f}C - resetting warnings")
                    self._thermal_warned = {"warn": False, "urgent": False}

            except Exception as e:
                logger.error(f"[thermal] monitor error: {e}")

            await asyncio.sleep(POLL_SEC)

    # Waiting on openai and livekit to support images for realtime models
    # Would work for cascade models
    # @function_tool
    # async def get_camera_image(self, context: RunContext):
    #     """Use this tool to take a picture with Pupster's camera and add it to the conversation."""
    #     return await self.tool_impl.get_camera_image(context)

    # all functions annotated with @function_tool will be passed to the LLM when this
    # agent is active

    @function_tool
    async def queue_activate_walking(self, context: RunContext):
        """Use this tool to activate your walking mode (you have 12 motors on your body, 3 per leg). This supports both 4-legged and 3-legged walking gaits."""
        logger.info("FUNCTION CALL: queue_activate_walking()")
        return await self.tool_impl.queue_activate_walking()

    @function_tool
    async def queue_deactivate(self, context: RunContext):
        """Use this tool to deactivate your motors."""
        return await self.tool_impl.queue_deactivate()

    @function_tool
    async def queue_move_in_direction(self, context: RunContext, heading: float, speed: float, duration: float):
        """Use this tool to queue up a move command which makes the robot walk in a certain direction (heading) at a certain speed for a certain amount of time.

        This function first queues a turn to the specified heading, then moves forward at the specified speed for the given duration.

        Examples:
            To move at a NW heading for 1 meter, call immediate_stop() and then queue_move_in_direction(heading=-45, speed=0.5, duration=2)
            To move at a SE heading for 0.5 meters, call immediate_stop() and then queue_move_in_direction(heading=135, speed=0.25, duration=2)

        Args:
            heading (float): The direction in which the robot should move, in degrees. 0 degrees is forward, 90 is right, -90 is left, and 180/-180 is backward.
            speed (float): The speed at which the robot should move, in meters per second. Should be between 0.3 and 0.75.
            duration (float): The duration for which to apply the movement, in seconds.
        """
        logger.info(f"FUNCTION CALL: queue_move_in_direction(heading={heading}, speed={speed}, duration={duration})")
        turn_velocity = 90.0
        wz = -turn_velocity * (1 if heading > 0 else -1)
        turn_duration = abs(heading / turn_velocity)
        await self.tool_impl.queue_move_for_time(vx=0.0, vy=0.0, wz=wz, duration=turn_duration)
        await self.tool_impl.queue_move_for_time(vx=speed, vy=0.0, wz=0.0, duration=duration)

    @function_tool
    async def queue_move(
        self,
        context: RunContext,
        forward_backward_velocity: float,
        right_left_velocity: float,
        turning_velocity: float,
        duration: float,
    ):
        """Use this tool to queue up a move command which makes the robot walk with a certain body velocity for a certain amount of time.
        This puts a move request at the end of the command queue to be executed as soon as the other commands are done.
        You can queue up multiple moves to accomplish complex movement like a dance.

        If the user specifies a certain angle (e.g. turn 180 degrees to the right), you will need 1) come up with a reasonable duration (eg 3 seconds) and
        2) divide the target angle by the duration to get the angular velocity (turning_velocity = 60 degrees per second).

        Args:
            forward_backward_velocity (float): The velocity in the forward/backward direction [meters per second]. Should be 0 or 0.4 < |forward_backward_velocity| < 0.75. Positive values move forward, negative backward.
            right_left_velocity (float): The velocity in the right/left direction [meters per second]. Should be 0 or 0.4 < |right_left_velocity| < 0.5. Positive values move to the right, negative to the left.
            turning_velocity (float): The angular velocity around the z axis [degrees per second]. Should be 0 or 30 < |turning_velocity| < 120. Positive values turn right, negative turn left.
            duration (float): The duration for which to apply the movement, in seconds.

        Example:
            To spin 90 degrees to the right, you could call: queue_move(forward_backward_velocity=0.0, right_left_velocity=0.0, turning_velocity=90.0, duration=1.0)
            To walk forwards and to the right, you could call: queue_move(forward_backward_velocity=0.5, right_left_velocity=0.3, turning_velocity=0.0, duration=1.0)
            To strafe left, you could call: queue_move(forward_backward_velocity=0.0, right_left_velocity=-0.4, turning_velocity=0.0, duration=1.0)
            To turn 360 degrees to the left while moving forward, you could call: queue_move(forward_backward_velocity=0.5, right_left_velocity=0.0, turning_velocity=-90.0, duration=4.0)

            ```
            User: "Can you do a dance?"
            Pupster: "Of course! I love to dance."
            Pupster calls functions:
            immediate_stop()
            queue_move(forward_backward_velocity=0, right_left_velocity=0.5, turning_velocity=0, duration=2)
            queue_move(forward_backward_velocity=0, right_left_velocity=-0.5, turning_velocity=0, duration=2)
            queue_move(forward_backward_velocity=0, right_left_velocity=0, turning_velocity=90, duration=2)
            queue_move(forward_backward_velocity=0, right_left_velocity=0, turning_velocity=-90, duration=2)
            ```

            * More examples of how to use queue_move
            ```
            User: "Can you go left for 1 meter and then right for 1 meter?"
            Pupster: "I love this game!"
            Pupster calls functions:
            immediate_stop()
            queue_move(forward_backward_velocity=0.0, right_left_velocity=-0.3, turning_velocity=0.0, duration=3.33)
            queue_move(forward_backward_velocity=0.0, right_left_velocity=0.3, turning_velocity=0.0, duration=3.33)

            User: "Stop!"
            Pupster calls functions:
            immediate_stop()
            Pupster: "Stopped"

            User: "Spin in a circle"
            Pupster calls functions:
            immediate_stop()
            queue_move(forward_backward_velocity=0, right_left_velocity=0, turning_velocity=90, duration=4)
            Pupster: I love spinning!
            ```

        Invalid commands:
            Small movements such as queue_move(forward_backward_velocity=0.1, right_left_velocity=0.0, turning_velocity=0.0, duration=2.0) are invalid and will be ignored because the real robot
            is not responsive to small velocities. Use 0 or a velocity above the threshold.
        """
        logger.info(
            f"FUNCTION CALL: queue_move(forward_backward_velocity={forward_backward_velocity}, right_left_velocity={right_left_velocity}, turning_velocity={turning_velocity}, duration={duration})"
        )

        return await self.tool_impl.queue_move_for_time(
            vx=forward_backward_velocity, vy=-right_left_velocity, wz=-turning_velocity, duration=duration
        )

    @function_tool
    async def queue_stop(self, context: RunContext):
        """Use this tool to queue a Stop command (zero all velocity) at the end of the command queue."""
        logger.info("FUNCTION CALL: queue_stop()")
        return await self.tool_impl.queue_stop()

    @function_tool
    async def queue_wait(self, context: RunContext, duration: float):
        """Use this tool to wait for a certain duration before executing the next command in the queue.

        Args:
            duration (float): The duration to wait, in seconds.
        """
        logger.info(f"FUNCTION CALL: Waiting for {duration} seconds")

        return await self.tool_impl.queue_wait(duration)

    @function_tool(
        description=f"""Use this tool to queue an animation sequence. This switches to a pre-recorded animation controller.

Available animations:
{chr(10).join([f'- "{name}": {data["description"]}' for name, data in ANIMATION_NAMES.items()])}

Args:
    animation_name (str): The name of the animation to play. Must be one of: {", ".join([f'"{name}"' for name in ANIMATION_NAMES.keys()])}

    If doing the pee animation, make sure you activate walking before and after to avoid falling over!

Example:
    To make the robot twerk: queue_animation(animation_name="twerk")
    To make the robot do a downward dog from lying position: queue_animation(animation_name="lie_downward_dog")
"""
    )
    async def queue_animation(self, context: RunContext, animation_name: str):
        logger.info(f"FUNCTION CALL: queue_animation(animation_name={animation_name})")

        # Validate animation name and resolve alias
        if animation_name not in ANIMATION_NAMES:
            raise ValueError(
                f"Unknown animation '{animation_name}'. Available animations: {list(ANIMATION_NAMES.keys())}"
            )

        actual_animation_name = ANIMATION_NAMES[animation_name]["csv_name"]

        # Queue the animation
        result = await self.tool_impl.queue_animation(actual_animation_name)

        # Calculate animation duration and queue a wait command
        duration = get_animation_duration(actual_animation_name)
        logger.info(f"Queueing wait for {duration:.2f} seconds for animation {animation_name}")
        await self.tool_impl.queue_wait(duration)

        # After the animation finishes, hand control back to the neural
        # (walking) controller so the robot stands gracefully instead of
        # locking up with stiff legs. (From Teresa's overlay; the lie_down
        # exception is kept for when that animation is added.)
        if animation_name != "lie_down":
            logger.info("Queueing activate_walking after animation to return control to neural controller")
            await self.tool_impl.queue_activate_walking()
        else:
            logger.info("lie_down animation: staying lying down, NOT re-activating walking")

        return result

    @function_tool
    async def reset_command_queue(self, context: RunContext):
        """Use this tool to remove all pending commands from the command queue."""
        logger.info(f"FUNCTION CALL: reset_command_queue()")

        return await self.tool_impl.clear_queue()

    @function_tool
    async def immediate_stop(self, context: RunContext):
        """Clears the command queue. Then interrupts and stops the executing command whatever it may be. Finanlly sends a Stop command (all velocities zero)."""
        logger.info(f"FUNCTION CALL: immediate_stop()")

        return await self.tool_impl.immediate_stop()

    @function_tool
    async def analyze_camera_image(self, prompt: str, context: RunContext):
        """Analyze the current camera image and return a description of objects in the scene.

        Examples:
        User asks: "Where I could find a banana"
        Call analyze_camera_image(prompt="Locate all bananas, or areas that could have bananas like kitchens or dining tables")

        User asks: "Where's the bathroom"
        Call analyze_camera_image(prompt="Locate the bathroom or any signs indicating its location")

        User asks: "Describe the scene generally"
        Call analyze_camera_image(prompt="Provide a detailed description of all visible objects, their positions, and any notable features")

        Args:
            prompt (str): A textual prompt to guide the analysis.

        Returns:
            A text description of the world including objects and their elevation (+ is above camera, - is below camera) and heading (+ is to the right, - is to the left).
        """
        logger.info(f"FUNCTION CALL: analyze_camera_image(prompt={prompt})")

        return await self.tool_impl.analyze_camera_image(prompt, context)

    @function_tool
    async def activate_person_following(self, context: RunContext):
        """Activate the person following behavior."""
        return await self.tool_impl.activate_person_following()

    @function_tool
    async def deactivate_person_following(self, context: RunContext):
        """Deactivate the person following behavior."""
        return await self.tool_impl.deactivate_person_following()

    @function_tool
    async def set_speaker_volume(self, volume: int, context: RunContext):
        """Set the speaker volume to specific level
        Args:
            volume (int): Volume level between 0 and 150. Only set below 50 if specified to be silent/off.

        Volume guidelines:
        * off: volume=0
        * very quiet: volume=50
        * quiet: volume=75
        * normal: volume=100
        * loud: volume=125
        * very loud: volume=150
        """
        logger.info(f"FUNCTION CALL: set_speaker_volume()")
        try:
            # Convert volume (0-150) to level (0.0-1.5)
            level = max(0.0, min(volume / 150 * 1.5, 1.5))
            logger.info(f"Setting speaker volume to {level} (input volume {volume})")
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_SINK@", str(level)], check=True)
            return f"Speaker volume set to {volume}%."
        except Exception as e:
            logger.error(f"Failed to set speaker volume: {e}")
            return f"Failed to set speaker volume: {e}"

    @function_tool
    async def check_mode(self, context: RunContext):
        """Determines if we are in Walking/Following mode, Animation mode, or Idle mode.

        Note that walking mode and following mode are not discernible from each other via this function.
        """
        return await self.tool_impl.check_mode()

    @function_tool
    async def rest(self, context: RunContext):
        """Enter rest mode to save battery and cool down. Pauses your vision system (camera + person detection), which is your single biggest power draw and heat source. You can still hear and talk while resting - only your eyes turn off. Use when the user says 'rest', 'take a nap', 'take a break', 'go to sleep', 'power save', 'you can rest now', or when you'll be idle on your stand for a while. Call wake_up to see again."""
        logger.info("FUNCTION CALL: rest()")
        return await self.tool_impl.rest()

    @function_tool
    async def wake_up(self, context: RunContext):
        """Exit rest mode and resume your vision system (camera + person detection) so you can see again. Use when the user says 'wake up', 'wake', 'you can look now', 'open your eyes', or wants you to see/follow them after you were resting."""
        logger.info("FUNCTION CALL: wake_up()")
        return await self.tool_impl.wake()

    @function_tool
    async def set_personality(self, context: RunContext, personality: str):
        """Switch your personality. Options: 'pupster' (spunky, chaotic, a little snarky - the original) or 'bumblebee' (warm, wise, gentle). Use when the user says things like 'be Bumblebee', 'switch to Pupster', 'change your personality', or 'become the other one'. You briefly restart (a few seconds) and then greet them in the new personality."""
        logger.info(f"FUNCTION CALL: set_personality({personality})")
        p = personality.strip().lower()
        if p not in _VALID_PERSONAS:
            return f"I can be 'pupster' or 'bumblebee', but I didn't recognize '{personality}'."
        set_persona_env(p)
        _schedule_agent_restart()
        return f"Switching to my {p} personality now - give me just a moment to become them!"

    @function_tool
    async def set_voice(self, context: RunContext, voice_name: str):
        """Switch your speaking voice instantly. Known voices: 'dug' (your default dog voice), plus any custom voices cloned in the voice demo (for example 'mark'). Use when the user says 'talk like Mark', 'use your Dug voice', 'change your voice', or 'sound like <name>'. The change is immediate - your very next words are in the new voice."""
        logger.info(f"FUNCTION CALL: set_voice({voice_name})")
        vid = resolve_voice_id(voice_name)
        if vid is None:
            return f"I don't have a voice called '{voice_name}'. I have my Dug voice, plus any you've cloned in the voice demo."
        set_voice_file(vid)  # persist so a future restart keeps this voice
        # Live switch on the running session's TTS - no restart, so the very
        # next spoken words (this confirmation) are already in the new voice.
        try:
            self.session.tts.update_options(voice=vid)
            return f"There - I'm using the {voice_name} voice now! How do I sound?"
        except Exception as e:
            logger.warning(f"live voice switch failed ({e}); falling back to restart")
            _schedule_agent_restart()
            return f"Switching to the {voice_name} voice - give me just a moment!"
