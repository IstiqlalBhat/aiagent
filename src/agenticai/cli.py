"""CLI interface for Agentic AI."""

import asyncio
import os
import sys
from typing import Optional
from pathlib import Path

import click
import structlog
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from .core.config import load_config, load_schedules, Config
from .core.call_manager import CallManager
from .scheduler.scheduler import CallScheduler
from .server.app import run_server
from .gateway.client import GatewayClient
from .gateway.messages import GatewayMessage

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


@cli.command("bot")
@click.option("--gateway-url", "-g", default=None, help="OpenClaw Gateway WebSocket URL")
@click.pass_context
def clawdbot(ctx, gateway_url):
    """Connect to OpenClaw Gateway - interactive terminal mode.
    
    This connects your AI phone agent to the OpenClaw Gateway for real-time
    communication. OpenClaw (github.com/openclaw/openclaw) uses a Gateway
    WebSocket for all channel integrations.
    
    The agent sends call events (transcripts, outcomes) to OpenClaw, allowing
    you to monitor and control phone calls from any OpenClaw interface.
    
    Examples:
        agenticai bot
        agenticai bot --gateway-url ws://localhost:18789
    """
    config = get_config(ctx.obj["config_path"])
    
    url = gateway_url or os.environ.get("GATEWAY_URL") or config.gateway.url
    
    console.print(Panel.fit(
        "[bold cyan]🦞 OpenClaw Gateway Terminal[/bold cyan]\n"
        f"Connecting to: [yellow]{url}[/yellow]\n\n"
        "[dim]Type 'help' for commands, 'quit' to exit[/dim]",
        border_style="cyan"
    ))
    
    async def run_bot():
        # Create gateway client
        gateway = GatewayClient(
            url=url,
            max_reconnect_attempts=config.gateway.reconnect_max_attempts,
            reconnect_base_delay=config.gateway.reconnect_base_delay,
            reconnect_max_delay=config.gateway.reconnect_max_delay,
        )
        
        # Track connection state
        connected = False
        messages_received = []
        
        async def connect_gateway():
            nonlocal connected
            try:
                await gateway.connect()
                connected = True
            except Exception as e:
                console.print(f"[red]Connection failed: {e}[/red]")
        
        # Start connection in background
        connect_task = asyncio.create_task(connect_gateway())
        
        # Wait briefly for connection
        await asyncio.sleep(2)
        
        if gateway.is_connected:
            console.print("[green]✓ Connected to OpenClaw Gateway[/green]")
        else:
            console.print("[yellow]⚠ Connecting to OpenClaw in background...[/yellow]")
        
        console.print()
        
        # Interactive loop
        while True:
            try:
                # Use synchronous input in async context
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: console.input("[bold cyan]openclaw>[/bold cyan] ")
                )
                
                cmd = user_input.strip().lower()
                
                if cmd in ("quit", "exit", "q"):
                    console.print("[dim]Goodbye! 🦞[/dim]")
                    break
                
                elif cmd == "help":
                    console.print(Panel(
                        "[bold]Available Commands:[/bold]\n\n"
                        "  [cyan]status[/cyan]     - Check OpenClaw Gateway connection\n"
                        "  [cyan]call[/cyan]       - Initiate a phone call\n"
                        "  [cyan]active[/cyan]     - List active calls\n"
                        "  [cyan]ping[/cyan]       - Send heartbeat to gateway\n"
                        "  [cyan]config[/cyan]     - Show current configuration\n"
                        "  [cyan]clear[/cyan]      - Clear screen\n"
                        "  [cyan]quit[/cyan]       - Exit terminal\n\n"
                        "[dim]OpenClaw Gateway: github.com/openclaw/openclaw[/dim]",
                        title="🦞 OpenClaw Commands",
                        border_style="blue"
                    ))
                
                elif cmd == "status":
                    status = "🟢 Connected" if gateway.is_connected else "🔴 Disconnected"
                    console.print(f"[bold]OpenClaw Gateway:[/bold] {status}")
                    console.print(f"[dim]URL: {url}[/dim]")
                
                elif cmd == "ping":
                    if gateway.is_connected:
                        from .gateway.messages import HeartbeatMessage
                        await gateway.send_message(HeartbeatMessage())
                        console.print("[green]Ping sent![/green]")
                    else:
                        console.print("[red]Not connected to gateway[/red]")
                
                elif cmd == "config":
                    console.print(Panel(
                        f"[bold]Twilio:[/bold] {config.twilio.from_number}\n"
                        f"[bold]Gemini Model:[/bold] {config.gemini.model}\n"
                        f"[bold]Gateway:[/bold] {config.gateway.url}\n"
                        f"[bold]Server:[/bold] {config.server.host}:{config.server.port}",
                        title="Current Configuration",
                        border_style="green"
                    ))
                
                elif cmd == "call":
                    console.print("[yellow]Use 'agenticai trigger' for quick calls[/yellow]")
                    console.print("Example: agenticai trigger --to +15551234567")
                
                elif cmd == "active":
                    console.print("[dim]Active calls shown via 'agenticai status'[/dim]")
                
                elif cmd == "clear":
                    console.clear()
                
                elif cmd:
                    # Send as raw message to gateway
                    console.print(f"[dim]Unknown command: {cmd}. Type 'help' for available commands.[/dim]")
                
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye! 🦞[/dim]")
                break
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
        
        # Cleanup
        await gateway.disconnect()
        connect_task.cancel()
        try:
            await connect_task
        except asyncio.CancelledError:
            pass
    
    asyncio.run(run_bot())


@cli.command("trigger")
@click.option("--to", "-t", required=True, help="Phone number to call (E.164 format)")
@click.option("--prompt", "-p", default=None, help="Custom prompt (optional)")
@click.option("--webhook-url", "-w", default=None, help="Webhook URL (uses NGROK_URL env if not set)")
@click.option("--server-url", "-s", default="http://localhost:8080", help="Server URL")
@click.pass_context
def trigger_call(ctx, to, prompt, webhook_url, server_url):
    """Quick trigger a phone call manually.
    
    This calls the running server's API to initiate a call, ensuring
    the Gemini audio bridge works correctly.
    
    NOTE: The server must be running first (agenticai server)
    
    Examples:
        agenticai trigger --to +15551234567
        agenticai trigger -t +15551234567 -p "Ask about their appointment"
    """
    import httpx
    
    config = get_config(ctx.obj["config_path"])
    
    # Get webhook URL from env if not provided
    if not webhook_url:
        webhook_url = os.environ.get("NGROK_URL")
        if not webhook_url:
            console.print("[red]Error: --webhook-url required or set NGROK_URL environment variable[/red]")
            console.print("\n[dim]Example: export NGROK_URL=https://your-subdomain.ngrok.io[/dim]")
            sys.exit(1)
    
    # Use default prompt if not provided
    if not prompt:
        prompt = config.gemini.system_instruction or "You are a helpful AI assistant making a phone call."
    
    console.print(Panel.fit(
        f"[bold]📞 Triggering Call[/bold]\n\n"
        f"To: [cyan]{to}[/cyan]\n"
        f"From: [green]{config.twilio.from_number}[/green]\n"
        f"Webhook: [yellow]{webhook_url}[/yellow]",
        border_style="blue"
    ))
    
    # Check if server is running
    try:
        with httpx.Client(timeout=5) as client:
            health = client.get(f"{server_url}/health")
            if health.status_code != 200:
                raise Exception("Server not healthy")
    except Exception:
        console.print(f"\n[red]✗ Server not running at {server_url}[/red]")
        console.print("[dim]Start the server first: agenticai server[/dim]")
        sys.exit(1)
    
    # Call the server API
    try:
        with console.status("[bold green]Initiating call via server...", spinner="dots"):
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    f"{server_url}/api/call",
                    json={
                        "to": to,
                        "prompt": prompt,
                        "webhook_url": webhook_url,
                    }
                )
                result = response.json()
        
        if result.get("success"):
            console.print(f"\n[green]✓ Call initiated![/green]")
            console.print(f"  Call ID: [cyan]{result['call_id']}[/cyan]")
            console.print("\n[dim]The call is in progress. Check server logs for details.[/dim]")
        else:
            console.print(f"\n[red]✗ Error: {result.get('error', 'Unknown error')}[/red]")
            
    except Exception as e:
        console.print(f"\n[red]✗ Error: {e}[/red]")


@cli.command("setup")
@click.pass_context
def setup_wizard(ctx):
    """Interactive setup wizard for Twilio and Gemini credentials.
    
    This will guide you through setting up your .env file with
    all the required credentials for Agentic AI.
    """
    console.print(Panel.fit(
        "[bold cyan]🔧 Agentic AI Setup Wizard[/bold cyan]\n\n"
        "This wizard will help you configure:\n"
        "  • Twilio credentials\n"
        "  • Gemini API key\n"
        "  • Gateway settings\n"
        "  • Server configuration",
        border_style="cyan"
    ))
    console.print()
    
    env_path = Path(".env")
    env_content = {}
    
    # Check for existing .env
    if env_path.exists():
        if not Confirm.ask("[yellow]A .env file already exists. Overwrite?[/yellow]"):
            console.print("[dim]Setup cancelled.[/dim]")
            return
    
    console.print("[bold]Step 1: Twilio Configuration[/bold]")
    console.print("[dim]Get these from https://console.twilio.com/[/dim]\n")
    
    account_sid = Prompt.ask("  Twilio Account SID", default="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    auth_token = Prompt.ask("  Twilio Auth Token", password=True)
    phone_number = Prompt.ask("  Your Twilio Phone Number (E.164 format)", default="+1XXXXXXXXXX")
    
    env_content["TWILIO_ACCOUNT_SID"] = account_sid
    env_content["TWILIO_AUTH_TOKEN"] = auth_token
    env_content["TWILIO_PHONE_NUMBER"] = phone_number
    
    console.print()
    console.print("[bold]Step 2: Google Gemini Configuration[/bold]")
    console.print("[dim]Get your API key from https://aistudio.google.com/apikey[/dim]\n")
    
    gemini_key = Prompt.ask("  Gemini API Key", password=True)
    env_content["GEMINI_API_KEY"] = gemini_key
    
    console.print()
    console.print("[bold]Step 3: Gateway Configuration[/bold]")
    console.print("[dim]OpenClaw Gateway WebSocket URL[/dim]\n")
    
    gateway_url = Prompt.ask("  Gateway URL", default="ws://127.0.0.1:18789")
    env_content["GATEWAY_URL"] = gateway_url
    
    console.print()
    console.print("[bold]Step 4: Server Configuration[/bold]")
    console.print("[dim]Local server settings for Twilio webhooks[/dim]\n")
    
    server_host = Prompt.ask("  Server Host", default="0.0.0.0")
    server_port = Prompt.ask("  Server Port", default="8080")
    
    env_content["SERVER_HOST"] = server_host
    env_content["SERVER_PORT"] = server_port
    
    console.print()
    console.print("[bold]Step 5: ngrok Configuration (Optional)[/bold]")
    console.print("[dim]Public URL for Twilio webhooks[/dim]\n")
    
    ngrok_url = Prompt.ask("  ngrok URL (leave empty to skip)", default="")
    if ngrok_url:
        env_content["NGROK_URL"] = ngrok_url
    
    # Write .env file
    console.print()
    with console.status("[bold green]Writing .env file..."):
        with open(env_path, "w") as f:
            f.write("# Agentic AI Configuration\n")
            f.write("# Generated by setup wizard\n\n")
            for key, value in env_content.items():
                f.write(f"{key}={value}\n")
    
    # Also update config.yaml with the phone number
    config_path = Path("config.yaml")
    if config_path.exists():
        try:
            config_content = config_path.read_text()
            if '+1XXXXXXXXXX' in config_content:
                updated_content = config_content.replace('+1XXXXXXXXXX', phone_number)
                config_path.write_text(updated_content)
                console.print("[green]✓ Updated config.yaml with phone number[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠ Could not update config.yaml: {e}[/yellow]")
    
    console.print()
    console.print(Panel.fit(
        "[bold green]✓ Setup Complete![/bold green]\n\n"
        "Next steps:\n"
        "  1. Start ngrok: [cyan]ngrok http 8080[/cyan]\n"
        "  2. Update NGROK_URL in .env with your ngrok URL\n"
        "  3. Start the server: [cyan]agenticai server[/cyan]\n"
        "  4. Make a call: [cyan]agenticai trigger --to +15551234567[/cyan]\n\n"
        "[dim]Or connect to ClawdBot: agenticai bot[/dim]",
        border_style="green"
    ))


@cli.command("test-connection")
@click.pass_context
def test_connection(ctx):
    """Test connections to Twilio, Gemini, and Gateway.
    
    Verifies that all services are reachable and credentials are valid.
    """
    config = get_config(ctx.obj["config_path"])
    
    console.print(Panel.fit(
        "[bold]🔍 Testing Connections[/bold]",
        border_style="blue"
    ))
    console.print()
    
    async def run_tests():
        results = []
        
        # Test Twilio
        console.print("[dim]Testing Twilio...[/dim]")
        try:
            from twilio.rest import Client
            client = Client(config.twilio.account_sid, config.twilio.auth_token)
            account = client.api.accounts(config.twilio.account_sid).fetch()
            results.append(("Twilio", "✓ Connected", f"Account: {account.friendly_name}"))
            console.print("[green]  ✓ Twilio OK[/green]")
        except Exception as e:
            results.append(("Twilio", "✗ Failed", str(e)[:50]))
            console.print(f"[red]  ✗ Twilio Failed: {e}[/red]")
        
        # Test Gateway
        console.print("[dim]Testing Gateway...[/dim]")
        try:
            gateway = GatewayClient(url=config.gateway.url)
            # Try to connect with timeout
            connect_task = asyncio.create_task(gateway.connect())
            await asyncio.sleep(3)
            
            if gateway.is_connected:
                results.append(("Gateway", "✓ Connected", config.gateway.url))
                console.print("[green]  ✓ Gateway OK[/green]")
            else:
                results.append(("Gateway", "⚠ Pending", "Connection in progress"))
                console.print("[yellow]  ⚠ Gateway connecting...[/yellow]")
            
            await gateway.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
        except Exception as e:
            results.append(("Gateway", "✗ Failed", str(e)[:50]))
            console.print(f"[red]  ✗ Gateway Failed: {e}[/red]")
        
        # Test Gemini (just check API key format)
        console.print("[dim]Checking Gemini config...[/dim]")
        if config.gemini.api_key and len(config.gemini.api_key) > 10:
            results.append(("Gemini", "✓ Configured", f"Model: {config.gemini.model}"))
            console.print("[green]  ✓ Gemini configured[/green]")
        else:
            results.append(("Gemini", "✗ Missing", "API key not set"))
            console.print("[red]  ✗ Gemini API key not set[/red]")
        
        # Summary table
        console.print()
        table = Table(title="Connection Test Results")
        table.add_column("Service", style="cyan")
        table.add_column("Status")
        table.add_column("Details")
        
        for service, status, details in results:
            style = "green" if "✓" in status else ("yellow" if "⚠" in status else "red")
            table.add_row(service, f"[{style}]{status}[/{style}]", details)
        
        console.print(table)
    
    asyncio.run(run_tests())


def main():
    """Main entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
