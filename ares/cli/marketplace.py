"""Async `/skills` and `/mcp` marketplace commands for the terminal CLI."""

from __future__ import annotations

import asyncio
import copy
import getpass
import shlex
import sys
from pathlib import Path
from typing import Any

from rich.table import Table

from ares.config import save_config
from ares.mcp_registry import MCPRegistryClient
from ares.models import DEFAULT_MCP_SERVERS
from ares.skill_generator import SkillGenerationError, SkillGenerator
from ares.skill_registry import (
    RegistryError as SkillRegistryError,
    SafeSkillInstaller,
    SkillRegistryClient,
    SkillValidationError,
    marketplace_record,
)
from ares.skills import SkillManager

from .constants import CLI_BOX


class MarketplaceCommandMixin:
    """Commands that use network registries while keeping every mutation explicit."""

    async def _handle_marketplace_command(self, command_line: str) -> bool:
        """Handle new marketplace verbs and return whether a command was consumed."""
        try:
            parts = shlex.split(command_line, posix=True)
        except ValueError as exc:
            self.console.print(f"[red]Invalid command quoting: {exc}[/red]")
            return True
        if not parts:
            return False
        command = parts[0].casefold()
        if command == "/skills":
            await self._handle_skills_marketplace(parts[1:])
            return True
        if command == "/mcp" and len(parts) > 1 and parts[1].casefold() in {
            "search", "add", "list", "info", "remove", "test", "refresh"
        }:
            await self._handle_mcp_marketplace(parts[1:])
            return True
        return False

    async def _handle_skills_marketplace(self, tokens: list[str]) -> None:
        action = tokens[0].casefold() if tokens else "list"
        values, registry, yes = self._marketplace_options(tokens[1:])
        if action in {"list", "ls"} and not values:
            self._show_marketplace_skills()
        elif action == "categories" and not values:
            self._show_skill_categories()
        elif action == "load" and len(values) == 1:
            self._show_local_skill(values[0])
        elif action == "search" and values:
            await self._skills_search(" ".join(values), registry)
        elif action == "install" and len(values) == 1:
            await self._skills_install(values[0], registry=registry, yes=yes)
        elif action == "create" and values:
            await self._skills_create(values[0], " ".join(values[1:]))
        elif action == "info" and len(values) == 1:
            await self._skills_info(values[0], registry=registry)
        elif action == "update" and len(values) <= 1:
            await self._skills_update(values[0] if values else "", yes=yes)
        elif action in {"remove", "delete"} and len(values) == 1:
            await self._skills_remove(values[0], yes=yes)
        elif action == "publish" and len(values) == 1:
            await self._skills_publish(values[0], registry=registry or "clawhub", yes=yes)
        elif action == "login" and not values:
            await self._skills_login(registry or "clawhub")
        elif action == "whoami" and not values:
            await self._skills_whoami(registry or "clawhub")
        else:
            self.console.print(
                "[red]Usage: /skills [list|search QUERY|install SLUG|create NAME DESCRIPTION|"
                "info NAME|update [NAME]|remove NAME|publish NAME|login|whoami|categories|load NAME] "
                "[--registry NAME] [--yes][/red]"
            )

    async def _handle_mcp_marketplace(self, tokens: list[str]) -> None:
        action = tokens[0].casefold() if tokens else "list"
        values, registry, yes = self._marketplace_options(tokens[1:])
        if action in {"list", "ls"} and not values:
            self._show_marketplace_mcp_list()
        elif action == "search" and values:
            await self._mcp_search(" ".join(values), registry)
        elif action == "info" and len(values) == 1:
            await self._mcp_info(values[0], registry)
        elif action == "add" and len(values) == 1:
            await self._mcp_add(values[0], registry=registry, yes=yes)
        elif action in {"remove", "delete"} and len(values) == 1:
            await self._mcp_remove(values[0], yes=yes)
        elif action == "test" and len(values) <= 1:
            await self._mcp_test(values[0] if values else "")
        elif action == "refresh" and not values:
            await self._mcp_refresh()
        else:
            self.console.print(
                "[red]Usage: /mcp [search QUERY|add NAME|list|info NAME|remove NAME|test [NAME]|refresh] "
                "[--registry NAME] [--yes][/red]"
            )

    @staticmethod
    def _marketplace_options(tokens: list[str]) -> tuple[list[str], str | None, bool]:
        values: list[str] = []
        registry: str | None = None
        yes = False
        index = 0
        while index < len(tokens):
            value = tokens[index]
            if value == "--registry":
                if index + 1 >= len(tokens):
                    raise ValueError("--registry requires a registry name.")
                registry = tokens[index + 1]
                index += 2
            elif value in {"--yes", "-y"}:
                yes = True
                index += 1
            else:
                values.append(value)
                index += 1
        return values, registry, yes

    def _skill_client(self) -> SkillRegistryClient:
        return SkillRegistryClient(self.config.skill_registries)

    def _mcp_registry_client(self) -> MCPRegistryClient:
        return MCPRegistryClient(self.config.mcp_registries)

    def _show_marketplace_skills(self) -> None:
        skills = self.skill_manager.list_all()
        table = Table(title="Installed Skills", border_style="bright_magenta", box=CLI_BOX)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Category", no_wrap=True)
        table.add_column("Version", no_wrap=True)
        table.add_column("Source", no_wrap=True)
        table.add_column("Description", ratio=4)
        for skill in skills:
            origin = marketplace_record(skill) or {}
            table.add_row(
                skill.name,
                skill.category,
                skill.version,
                str(origin.get("registry") or "local"),
                skill.description,
            )
        self.console.print(table if skills else "[dim]No skills installed.[/dim]")

    def _show_skill_categories(self) -> None:
        categories = self.skill_manager.list_categories()
        table = Table(title="Skill Categories", border_style="bright_magenta", box=CLI_BOX)
        table.add_column("Category", style="cyan")
        table.add_column("Skills", justify="right")
        for name, count in categories.items():
            table.add_row(name, str(count))
        self.console.print(table if categories else "[dim]No skill categories found.[/dim]")

    def _show_local_skill(self, name: str) -> None:
        skill = self.skill_manager.get_skill(name)
        if skill is None:
            self.console.print(f"[red]Skill '{name}' is not installed.[/red]")
            return
        self._print_markdown_section(f"Skill: {skill.name}", skill.content, skill.description)

    async def _skills_search(self, query: str, registry: str | None) -> None:
        client = self._skill_client()
        try:
            results = await client.search(query, registry)
        except (ValueError, SkillRegistryError) as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if not results:
            self.console.print(f"[yellow]No marketplace skills found for '{query}'.[/yellow]")
            self._show_registry_errors(client.last_errors)
            return
        table = Table(title=f"Marketplace Skills: {query}", border_style="bright_magenta", box=CLI_BOX)
        table.add_column("Skill", style="cyan", no_wrap=True)
        table.add_column("Version", no_wrap=True)
        table.add_column("Publisher", no_wrap=True)
        table.add_column("Registry", no_wrap=True)
        table.add_column("Description", ratio=4)
        for result in results[:25]:
            label = result.reference if not result.suspicious else f"{result.reference} [flagged]"
            table.add_row(label, result.version, result.owner or "-", result.registry, result.description or "-")
        self.console.print(table)
        self._show_registry_errors(client.last_errors)

    async def _skills_info(self, name: str, registry: str | None) -> None:
        if registry is None:
            local = self.skill_manager.get_skill(name)
            if local is not None:
                self._show_local_skill(name)
                origin = marketplace_record(local)
                if origin:
                    self.console.print(f"[dim]Marketplace source: {origin.get('registry', '-')}/{origin.get('slug', '-')} {origin.get('version', '')}[/dim]")
                return
        client = self._skill_client()
        try:
            detail = await client.get_skill(name, registry)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if detail is None:
            self.console.print(f"[yellow]Skill '{name}' was not found in the configured registries.[/yellow]")
            self._show_registry_errors(client.last_errors)
            return
        table = Table(title=f"Marketplace Skill: {detail.name}", border_style="bright_magenta", box=CLI_BOX)
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", ratio=4)
        table.add_row("Slug", detail.slug)
        table.add_row("Version", detail.version)
        table.add_row("Publisher", detail.owner or "-")
        table.add_row("Registry", detail.registry)
        table.add_row("Security", detail.security_status + (" (flagged)" if detail.suspicious else ""))
        table.add_row("Dependencies", ", ".join(f"{item.type}:{item.name}" for item in detail.dependencies) or "none declared")
        table.add_row("Files", ", ".join(detail.files[:10]) or "not listed")
        if detail.canonical_url:
            table.add_row("Source", detail.canonical_url)
        table.add_row("Description", detail.description or "-")
        self.console.print(table)

    async def _skills_install(self, slug: str, *, registry: str | None, yes: bool) -> None:
        client = self._skill_client()
        try:
            detail = await client.get_skill(slug, registry)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if detail is None:
            self.console.print(f"[red]Skill '{slug}' was not found in the configured registries.[/red]")
            self._show_registry_errors(client.last_errors)
            return
        if detail.suspicious:
            self.console.print(
                "[red]Install blocked: this registry record is marked suspicious. Review the source manually before trusting it.[/red]"
            )
            return
        self.console.print(f"[cyan]Downloading {detail.slug} from {detail.registry}…[/cyan]")
        archive = await client.download(detail.reference, detail.version, detail.registry)
        if archive is None:
            self.console.print("[red]The registry did not provide a safe hosted ZIP to install.[/red]")
            self._show_registry_errors(client.last_errors)
            return
        installer = SafeSkillInstaller(Path(self.config.skill_dirs[0]).expanduser())
        try:
            installation = installer.install(
                archive,
                provenance={
                    "registry": detail.registry,
                    "slug": detail.reference,
                    "version": detail.version,
                    "canonical_url": detail.canonical_url,
                },
            )
        except (FileExistsError, SkillValidationError) as exc:
            self.console.print(f"[yellow]Skill was not installed: {exc}[/yellow]")
            return
        self.skill_manager = SkillManager(skill_dirs=list(self.config.skill_dirs or []) or None)
        self.console.print(f"[green]Installed {installation.skill.name}.[/green] [dim]{installation.path}[/dim]")
        missing = [
            dependency for dependency in installation.dependencies
            if dependency.type == "mcp_server" and not self._mcp_is_configured(dependency.name)
        ]
        for dependency in missing:
            self.console.print(
                f"[yellow]This skill requires MCP server '{dependency.name}'. It has not been added automatically.[/yellow]"
            )
            await self._mcp_add(dependency.name, registry=None, yes=yes, dependency_for=installation.skill.name)

    async def _skills_create(self, name: str, description: str) -> None:
        if not description:
            if not sys.stdin.isatty():
                self.console.print("[red]Usage: /skills create NAME DESCRIPTION[/red]")
                return
            self.console.print("[cyan]Describe what this skill should do:[/cyan]")
            description = await asyncio.to_thread(input, "> ")
        try:
            skill = await SkillGenerator(self.agent.llm).generate(name, description)
            location = SkillGenerator(self.agent.llm).save_skill(skill, Path(self.config.skill_dirs[0]).expanduser())
        except (FileExistsError, SkillGenerationError, ValueError) as exc:
            self.console.print(f"[red]Could not create skill: {exc}[/red]")
            return
        self.skill_manager = SkillManager(skill_dirs=list(self.config.skill_dirs or []) or None)
        self.console.print(f"[green]Created {skill.name}.[/green] [dim]Use /skills load {skill.name} · {location}[/dim]")

    async def _skills_update(self, name: str, *, yes: bool) -> None:
        targets = [self.skill_manager.get_skill(name)] if name else self.skill_manager.list_all()
        targets = [skill for skill in targets if skill is not None and marketplace_record(skill)]
        if not targets:
            self.console.print("[yellow]No installed marketplace skills are available to update.[/yellow]")
            return
        updated = 0
        for skill in targets:
            record = marketplace_record(skill) or {}
            registry = str(record.get("registry") or "")
            slug = str(record.get("slug") or "")
            if not registry or not slug:
                continue
            client = self._skill_client()
            detail = await client.get_skill(slug, registry)
            if detail is None or detail.suspicious:
                self.console.print(f"[yellow]Skipped {skill.name}: source is unavailable or flagged.[/yellow]")
                continue
            archive = await client.download(detail.reference, detail.version, registry)
            if archive is None:
                self.console.print(f"[yellow]Skipped {skill.name}: no safe hosted ZIP is available.[/yellow]")
                continue
            try:
                SafeSkillInstaller(Path(self.config.skill_dirs[0]).expanduser()).install(
                    archive,
                    provenance={"registry": registry, "slug": detail.reference, "version": detail.version, "canonical_url": detail.canonical_url},
                    replace=True,
                )
                updated += 1
            except SkillValidationError as exc:
                self.console.print(f"[yellow]Skipped {skill.name}: {exc}[/yellow]")
        self.skill_manager = SkillManager(skill_dirs=list(self.config.skill_dirs or []) or None)
        self.console.print(f"[green]Updated {updated} marketplace skill(s).[/green]")

    async def _skills_remove(self, name: str, *, yes: bool) -> None:
        skill = self.skill_manager.get_skill(name)
        if skill is None or not self.skill_manager.is_editable(skill):
            self.console.print(f"[red]Only user-installed skills can be removed; '{name}' was not found there.[/red]")
            return
        if not await self._confirm_marketplace(f"Remove local skill '{skill.name}'?", yes=yes):
            return
        self.skill_manager.delete_skill(skill.name)
        self.console.print(f"[green]Removed {skill.name}.[/green]")

    async def _skills_login(self, registry_name: str) -> None:
        client = self._skill_client()
        registry = client.configured_registry(registry_name)
        if registry is None:
            self.console.print(f"[red]Registry '{registry_name}' is not configured or enabled.[/red]")
            return
        if not sys.stdin.isatty():
            self.console.print("[yellow]Set the registry auth_token in ~/.ares/config.json, then run /skills whoami.[/yellow]")
            return
        token = await asyncio.to_thread(getpass.getpass, f"{registry.name} token (input hidden): ")
        if not token.strip():
            self.console.print("[yellow]No token saved.[/yellow]")
            return
        for item in self.config.skill_registries:
            if item.name.casefold() == registry.name.casefold():
                item.auth_token = token.strip()
        save_config(self.config)
        self.console.print(f"[green]Saved the {registry.name} token locally (it will be redacted from exports).[/green]")
        await self._skills_whoami(registry.name)

    async def _skills_whoami(self, registry_name: str) -> None:
        client = self._skill_client()
        try:
            identity = await client.whoami(registry_name)
        except (ValueError, SkillRegistryError) as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if identity:
            self.console.print(f"[green]Authenticated as {identity} on {registry_name}.[/green]")
        else:
            self.console.print(f"[yellow]No token configured for {registry_name}. Run /skills login.[/yellow]")

    async def _skills_publish(self, name: str, *, registry: str, yes: bool) -> None:
        skill = self.skill_manager.get_skill(name)
        if skill is None or not self.skill_manager.is_editable(skill):
            self.console.print("[red]Only a local user skill can be published.[/red]")
            return
        if skill.lint_messages:
            self.console.print("[red]Fix the skill lint before publishing:\n- " + "\n- ".join(skill.lint_messages) + "[/red]")
            return
        if not await self._confirm_marketplace(f"Publish '{skill.name}' publicly to {registry}?", yes=yes):
            return
        try:
            response = await self._skill_client().publish(skill=skill, registry=registry)
        except (ValueError, SkillRegistryError) as exc:
            self.console.print(f"[red]Publish failed: {exc}[/red]")
            return
        self.console.print(f"[green]Published {skill.name}.[/green] [dim]{response.get('url') or response.get('slug') or ''}[/dim]")

    def _show_marketplace_mcp_list(self) -> None:
        manager = getattr(self, "mcp_manager", None)
        report = manager.readiness_report() if manager is not None else {"servers": {}}
        servers = report.get("servers") or {}
        if not servers:
            self.console.print("[dim]No MCP servers are configured. Search with /mcp search QUERY.[/dim]")
            return
        table = Table(title="Configured MCP Servers", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Transport", no_wrap=True)
        table.add_column("Tools", justify="right")
        table.add_column("Details", ratio=3)
        for name, item in servers.items():
            table.add_row(
                name,
                "[green]ready[/green]" if item.get("ready") else "[yellow]disconnected[/yellow]",
                str(item.get("transport") or "-"),
                str(item.get("tools") or 0),
                str(item.get("error") or "-"),
            )
        self.console.print(table)

    async def _mcp_search(self, query: str, registry: str | None) -> None:
        client = self._mcp_registry_client()
        try:
            results = await client.search(query, registry)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if not results:
            self.console.print(f"[yellow]No MCP servers found for '{query}'.[/yellow]")
            self._show_registry_errors(client.last_errors)
            return
        table = Table(title=f"MCP Marketplace: {query}", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Server", style="cyan", no_wrap=True)
        table.add_column("Version", no_wrap=True)
        table.add_column("Registry", no_wrap=True)
        table.add_column("Trust", no_wrap=True)
        table.add_column("Description", ratio=4)
        for result in results[:25]:
            table.add_row(
                result.name,
                result.version,
                result.registry,
                "verified" if result.verified else "registry listing",
                result.description or "-",
            )
        self.console.print(table)
        self._show_registry_errors(client.last_errors)

    async def _mcp_info(self, name: str, registry: str | None) -> None:
        client = self._mcp_registry_client()
        try:
            detail = await client.get_server(name, registry)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if detail is None:
            self.console.print(f"[yellow]MCP server '{name}' was not found in the configured registries.[/yellow]")
            self._show_registry_errors(client.last_errors)
            return
        plan = await client.get_install_command(name, registry)
        table = Table(title=f"MCP Server: {detail.title or detail.name}", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", ratio=4)
        table.add_row("Name", detail.name)
        table.add_row("Version", detail.version)
        table.add_row("Registry", detail.registry)
        table.add_row("Trust", "verified" if detail.verified else "registry listing")
        table.add_row("Repository", detail.repository or "-")
        table.add_row("Install", self._format_plan(plan) if plan else "No safe automatic configuration is available.")
        table.add_row("Description", detail.description or "-")
        self.console.print(table)

    async def _mcp_add(self, name: str, *, registry: str | None, yes: bool, dependency_for: str = "") -> None:
        existing = {str(server.get("name") or "") for server in self.config.mcp_servers if isinstance(server, dict)}
        if name in existing:
            self.console.print(f"[green]MCP server '{name}' is already configured.[/green]")
            return
        builtin = next((item for item in DEFAULT_MCP_SERVERS if item["name"] == name), None)
        if builtin is not None:
            config = copy.deepcopy(builtin)
            source = "built-in Ares configuration"
            requirements: list[str] = []
        else:
            client = self._mcp_registry_client()
            try:
                plan = await client.get_install_command(name, registry)
            except ValueError as exc:
                self.console.print(f"[red]{exc}[/red]")
                return
            if plan is None:
                self.console.print(f"[yellow]No safe install plan was found for '{name}'. Review /mcp info {name} and configure it manually.[/yellow]")
                self._show_registry_errors(client.last_errors)
                return
            config = plan.as_config(existing_names=existing)
            source = f"{plan.registry} registry"
            requirements = list(plan.env_requirements)
        table = Table(title="Confirm MCP Configuration", border_style="yellow", box=CLI_BOX)
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", ratio=4)
        table.add_row("Server", config["name"])
        table.add_row("Source", source)
        table.add_row("Transport", config["transport"])
        table.add_row("Target", str(config.get("server_url") or config.get("command") or "-"))
        table.add_row("Arguments", " ".join(config.get("args") or []) or "-")
        table.add_row("Required secrets", ", ".join(requirements) or "none declared")
        if dependency_for:
            table.add_row("Required by", dependency_for)
        self.console.print(table)
        if not await self._confirm_marketplace(f"Add MCP server '{config['name']}' and allow it to connect?", yes=yes):
            return
        self.config.mcp_servers.append(config)
        save_config(self.config)
        await self._reconfigure_marketplace_mcp()
        self.console.print(
            f"[green]Added {config['name']}.[/green] "
            "[dim]Connection starts now; add any required secret values in ~/.ares/config.json before using it.[/dim]"
        )

    async def _mcp_remove(self, name: str, *, yes: bool) -> None:
        matches = [server for server in self.config.mcp_servers if isinstance(server, dict) and server.get("name") == name]
        if not matches:
            self.console.print(f"[red]MCP server '{name}' is not configured.[/red]")
            return
        if not await self._confirm_marketplace(f"Remove MCP server '{name}' from shared Ares config?", yes=yes):
            return
        self.config.mcp_servers = [
            server for server in self.config.mcp_servers
            if not isinstance(server, dict) or server.get("name") != name
        ]
        save_config(self.config)
        await self._reconfigure_marketplace_mcp()
        self.console.print(f"[green]Removed {name} from the shared MCP configuration.[/green]")

    async def _mcp_test(self, name: str) -> None:
        manager = getattr(self, "mcp_manager", None)
        if manager is None:
            self.console.print("[red]No MCP servers are configured.[/red]")
            return
        if name:
            report = await manager.reconnect_server(name)
            self._show_mcp_status(
                {"servers": {name: report}, "connected": int(report.get("ready", False)), "configured": 1, "tools": report.get("tools", 0)},
            )
        else:
            self._show_mcp_status(await manager.health_probe())
        if hasattr(self.agent, "refresh_tools"):
            self.agent.refresh_tools()

    async def _mcp_refresh(self) -> None:
        await self._reconfigure_marketplace_mcp(force=True)
        if self.mcp_manager is None:
            self.console.print("[red]No MCP servers are configured.[/red]")
            return
        self._show_mcp_status(self.mcp_manager.readiness_report())

    async def _reconfigure_marketplace_mcp(self, *, force: bool = False) -> None:
        self._mcp_config_signature = self._get_mcp_config_signature(self.config)
        self._mcp_reconfigure_pending = True
        if force:
            self._mcp_reconfigure_pending = True
        await self._refresh_mcp_manager_if_needed()

    def _mcp_is_configured(self, name: str) -> bool:
        wanted = str(name).casefold()
        return any(
            str(server.get("name") or "").casefold() == wanted
            for server in self.config.mcp_servers
            if isinstance(server, dict)
        )

    async def _confirm_marketplace(self, prompt: str, *, yes: bool) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            self.console.print("[yellow]Confirmation is required. Re-run this command with --yes after reviewing the plan.[/yellow]")
            return False
        answer = await asyncio.to_thread(input, f"{prompt} [y/N] ")
        return answer.strip().casefold() in {"y", "yes"}

    def _show_registry_errors(self, errors: dict[str, str]) -> None:
        for registry, error in sorted(errors.items()):
            self.console.print(f"[dim yellow]{registry}: {error}[/dim yellow]")

    @staticmethod
    def _format_plan(plan: Any) -> str:
        if plan is None:
            return "-"
        if plan.server_url:
            return f"{plan.transport} · {plan.server_url}"
        return f"{plan.transport} · {plan.command} {' '.join(plan.args)}".strip()
