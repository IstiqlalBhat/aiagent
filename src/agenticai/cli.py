"""CLI interface for Agentic AI."""

import asyncio
import sys
from typing import Optional

import click
import structlog
from rich.console import Console
from rich.table import Table

from .core.config import load_config, load_schedules, Config
from .core.call_manager import CallManager
from .scheduler.scheduler import CallScheduler
from .server.app import run_server

console = Console()

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer() if sys.stdout.isatty() else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


def get_config(config_path: Optional[str] = None) -> Config:
    """Load configuration."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        console.print("[red]Configuration file not found. Create config.yaml first.[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        sys.exit(1)


@click.group()
@click.option("--config", "-c", default=None, help="Path to config.yaml")
@click.pass_context
def cli(ctx, config):
    """Agentic AI - Twilio + Gemini + OpenClaw Gateway Integration."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.option("--to", "-t", required=True, help="Phone number to call (E.164 format)")
@click.option("--prompt", "-p", required=True, help="Prompt/instructions for the AI")
@click.option("--webhook-url", "-w", required=True, help="Public webhook base URL (e.g., https://example.ngrok.io)")
@click.pass_context
def call(ctx, to, prompt, webhook_url):
    """Initiate an outbound phone call."""
    config = get_config(ctx.obj["config_path"])

    console.print(f"[bold]Initiating call to {to}[/bold]")
    console.print(f"Prompt: {prompt[:50]}...")

    async def run_call():
        call_manager = CallManager(config)
        await call_manager.start()

        try:
            call_id = await call_manager.initiate_call(
                to_number=to,
                prompt=prompt,
                webhook_base_url=webhook_url,
            )
            console.print(f"[green]Call initiated![/green] Call ID: {call_id}")

            # Wait for call to complete
            console.print("Waiting for call to complete... (Ctrl+C to exit)")
            while call_id in call_manager.active_sessions:
                await asyncio.sleep(1)

            console.print("[green]Call completed.[/green]")

        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        finally:
            await call_manager.stop()

    asyncio.run(run_call())


@cli.group()
def schedule():
    """Manage scheduled calls."""
    pass


@schedule.command("list")
@click.pass_context
def schedule_list(ctx):
    """List all configured schedules."""
    schedules = load_schedules()

    table = Table(title="Configured Schedules")
    table.add_column("Name", style="cyan")
    table.add_column("Cron", style="green")
    table.add_column("Enabled", style="yellow")
    table.add_column("Calls", justify="right")

    for s in schedules.get("schedules", []):
        enabled = "Yes" if s.get("enabled", False) else "No"
        table.add_row(
            s.get("name", "unnamed"),
            s.get("cron", ""),
            enabled,
            str(len(s.get("calls", []))),
        )

    console.print(table)


@schedule.command("run")
@click.argument("name")
@click.option("--webhook-url", "-w", required=True, help="Public webhook base URL")
@click.pass_context
def schedule_run(ctx, name, webhook_url):
    """Run a schedule immediately."""
    config = get_config(ctx.obj["config_path"])

    console.print(f"[bold]Running schedule: {name}[/bold]")

    async def run_schedule():
        call_manager = CallManager(config)
        await call_manager.start()

        async def call_handler(to_number, prompt, metadata):
            return await call_manager.initiate_call(
                to_number=to_number,
                prompt=prompt,
                webhook_base_url=webhook_url,
                metadata=metadata,
            )

        scheduler = CallScheduler(call_handler)
        scheduler.load_schedules()

        try:
            call_ids = await scheduler.run_schedule_now(name)
            console.print(f"[green]Initiated {len(call_ids)} call(s)[/green]")

            for call_id in call_ids:
                console.print(f"  Call ID: {call_id}")

            # Wait for calls to complete
            if call_ids:
                console.print("Waiting for calls to complete... (Ctrl+C to exit)")
                while any(cid in call_manager.active_sessions for cid in call_ids):
                    await asyncio.sleep(1)

        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
        finally:
            await call_manager.stop()

    asyncio.run(run_schedule())


@cli.command()
@click.pass_context
def status(ctx):
    """Show system status."""
    config = get_config(ctx.obj["config_path"])

    table = Table(title="Agentic AI Status")
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Details")

    # Config status
    table.add_row(
        "Configuration",
        "Loaded",
        f"Twilio: {config.twilio.from_number}",
    )

    # Gemini config
    table.add_row(
        "Gemini",
        "Configured",
        f"Model: {config.gemini.model}",
    )

    # Gateway config
    table.add_row(
        "Gateway",
        "Configured",
        f"URL: {config.gateway.url}",
    )

    # Server config
    table.add_row(
        "Server",
        "Ready",
        f"http://{config.server.host}:{config.server.port}",
    )

    # Schedules
    schedules = load_schedules()
    enabled_count = sum(
        1 for s in schedules.get("schedules", [])
        if s.get("enabled", False)
    )
    total_count = len(schedules.get("schedules", []))
    table.add_row(
        "Schedules",
        f"{enabled_count} enabled",
        f"{total_count} total schedules",
    )

    console.print(table)


@cli.command()
@click.option("--host", "-h", default=None, help="Server host")
@click.option("--port", "-p", default=None, type=int, help="Server port")
@click.pass_context
def server(ctx, host, port):
    """Start the webhook server."""
    config = get_config(ctx.obj["config_path"])

    if host:
        config.server.host = host
    if port:
        config.server.port = port

    console.print(f"[bold]Starting server on {config.server.host}:{config.server.port}[/bold]")
    console.print(f"Webhook path: {config.server.webhook_path}")
    console.print(f"WebSocket path: {config.server.websocket_path}")

    run_server(config)


@cli.command()
@click.option("--webhook-url", "-w", required=True, help="Public webhook base URL")
@click.pass_context
def daemon(ctx, webhook_url):
    """Run server with scheduler (daemon mode)."""
    config = get_config(ctx.obj["config_path"])

    console.print("[bold]Starting Agentic AI in daemon mode[/bold]")

    async def run_daemon():
        import uvicorn
        from .server.app import create_app, get_call_manager

        # Create app
        app = create_app(config)

        # Start server in background
        server_config = uvicorn.Config(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level="info",
        )
        server = uvicorn.Server(server_config)

        # Create scheduler
        call_manager = get_call_manager()
        await call_manager.start()

        async def call_handler(to_number, prompt, metadata):
            return await call_manager.initiate_call(
                to_number=to_number,
                prompt=prompt,
                webhook_base_url=webhook_url,
                metadata=metadata,
            )

        scheduler = CallScheduler(call_handler)
        scheduler.load_schedules()
        scheduler.start()

        console.print("[green]Scheduler started[/green]")

        # Show next run times
        next_runs = scheduler.get_next_run_times()
        for name, time in next_runs.items():
            console.print(f"  {name}: {time}")

        try:
            await server.serve()
        finally:
            scheduler.stop()
            await call_manager.stop()

    asyncio.run(run_daemon())


def main():
    """Main entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
