import logging
import os
import sys
import argparse

# livekit-agents 1.6.5 calls logger.trace() in its STT/turn-taking paths, but
# the TRACE method is only registered by some CLI entrypoints. Without this
# shim every user utterance kills the STT consumer (AttributeError), so the
# agent speaks but never responds.
_TRACE_LEVEL = 5
if not hasattr(logging.Logger, "trace"):
    logging.addLevelName(_TRACE_LEVEL, "TRACE")

    def _trace(self, message, *args, **kwargs):
        if self.isEnabledFor(_TRACE_LEVEL):
            self._log(_TRACE_LEVEL, message, args, **kwargs)

    logging.Logger.trace = _trace

logging.getLogger("livekit.agents").setLevel(logging.WARNING)
logging.getLogger("livekit").setLevel(logging.WARNING)

from dotenv import load_dotenv
from livekit.agents import (
    NOT_GIVEN,
    AgentFalseInterruptionEvent,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    WorkerOptions,
    cli,
    metrics,
    ConversationItemAddedEvent,
    llm,
)
from livekit.agents import UserInputTranscribedEvent

from pupster import PupsterAgent, get_pupster_session
from ros_tool_server import RosToolServer

load_dotenv(".env.local")

logger = logging.getLogger("agent")


# Brain selection. Override without editing code via .env.local or the
# systemd unit, e.g. PUPSTER_AGENT_DESIGN=openai-cartesia
# Designs: qwen-spark (local Spark LLM, cloud fallback if OPENAI_API_KEY set),
#          openai-cartesia, openai-realtime, google-cartesia
AGENT_DESIGN = os.getenv("PUPSTER_AGENT_DESIGN", "qwen-spark")


def prewarm(proc: JobProcess):
    if AGENT_DESIGN == "cascade":
        from livekit.plugins import silero

        proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    session = get_pupster_session(AGENT_DESIGN)

    @session.on("conversation_item_added")
    def on_conversation_item_added(event: ConversationItemAddedEvent):
        # to iterate over all types of content:
        for content in event.item.content:
            if isinstance(content, str):
                logger.info(f" - text: {content}")
            elif isinstance(content, llm.ImageContent):
                # image is either a rtc.VideoFrame or URL to the image
                logger.info(f" - image: {content.image}")
            elif isinstance(content, llm.AudioContent):
                # frame is a list[rtc.AudioFrame]
                logger.info(f" - audio: {content.frame}, transcript: {content.transcript}")

    # sometimes background noise could interrupt the agent session, these are considered false positive interruptions
    # when it's detected, you may resume the agent's speech
    @session.on("agent_false_interruption")
    def _on_agent_false_interruption(ev: AgentFalseInterruptionEvent):
        logger.info("false positive interruption, resuming")
        session.generate_reply(instructions=ev.extra_instructions or NOT_GIVEN)

    # Metrics collection, to measure pipeline performance
    # For more information, see https://docs.livekit.io/agents/build/metrics/
    usage_collector = metrics.UsageCollector()

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event: UserInputTranscribedEvent):
        print(
            f"User input transcribed: {event.transcript}, "
            f"language: {event.language}, "
            f"final: {event.is_final}, "
            f"speaker id: {event.speaker_id}"
        )

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    ctx.add_shutdown_callback(log_usage)

    logger.info("Using RosToolServer")
    tool_impl = RosToolServer()

    await session.start(
        agent=PupsterAgent(tool_impl=tool_impl),
        room=ctx.room,
        room_input_options=RoomInputOptions(),
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    # Consume our own CLI flag and pass the rest to LiveKit CLI
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--tool-server",
        choices=["ros", "nop"],
        dest="tool_server",
        default="ros",
        help="Select tool server implementation: ros or nop",
    )
    args, passthrough = parser.parse_known_args(sys.argv[1:])

    if args.tool_server:
        os.environ["PUPSTER_TOOL_SERVER"] = args.tool_server

    # Rebuild argv so LiveKit CLI sees its expected args (e.g., 'console')
    sys.argv = [sys.argv[0]] + passthrough

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
