#SPDX:MIT
"""Parsing of ssh config files."""
from dataclasses import dataclass

from . import log
from .err import SshConfigErr
from .user_config import UserConfig


# ---------------------------------------------------------------------
# User SSH Setup Understanding.

@dataclass(frozen=True)
class SshConfigLine:
    """A single line from the ssh config file.
    
    Every line is either before the first 'Host' entry, or has a host value.
    'Host' lines have their own Host as their host value.

    Use a negative value for the 'line_no' to indicate it's an addition.

    The 'line' value MUST terminate in an EOL character.  This means rewriting
    the file can cause an extra EOL that wasn't there originally.
    """
    line: str
    line_no: int
    valid: bool
    host: str | None
    key: str | None
    value: str | None

    @staticmethod
    def parse_line(host: str | None, line: str, line_no: int) -> "SshConfigLine":
        """Parse the single line of the ssh_config file."""
        if line_no <= 0:
            raise ValueError("line_no must be positive")
        if line[-1] not in "\r\n":
            # See note above.
            line += "\n"
        in_scope = line.split("#")[0].strip()
        if not in_scope:
            return SshConfigLine(
                line=line, line_no=line_no, valid=True, host=host, key=None, value=None
            )
        idx = 0
        for val in in_scope:
            # Because of the strip() at the start and the 'if not in_scope' line,
            # idx will always be > 0 here, which means the extracted key is
            # already stripped.
            if val.isspace():
                key = in_scope[:idx]
                value = in_scope[idx:].strip()  # Note: should just need an 'lstrip()' here.
                if value and key.lower() == "host":
                    # Switch the host to this one.
                    return SshConfigLine(
                        line=line, line_no=line_no, valid=True, host=value, key=key, value=value
                    )
                return SshConfigLine(
                    line=line, line_no=line_no, valid=bool(value), host=host, key=key, value=value
                )
            idx += 1
        return SshConfigLine(
            line=line, line_no=line_no, valid=False, host=host, key=None, value=None
        )


class SshConfig:
    """Handler for reading and managing the ssh config file."""

    def __init__(self, hosts: dict[str | None, list[SshConfigLine]]) -> None:
        self.by_host = hosts
        self._last_new_line = 0

    def format_errors(self) -> list[SshConfigLine]:
        """Get all lines with validation problems."""
        ret: list[SshConfigLine] = []
        for lines in self.by_host.values():
            for line in lines:
                if not line.valid:
                    ret.append(line)
        # This expects that only existing, parsed lines have issues.
        return sorted(ret, key=lambda x: x.line_no)

    def has_host(self, host: str) -> bool:
        """Test if the config file has the host block."""
        return host in self.by_host

    def add_key_value(self, host: str, key: str, value: str) -> None:
        """Add the key/value to the host block.
        
        The host block must exist.
        """
        if host not in self.by_host:
            raise SshConfigErr(f"ssh config does not have Host block {host}")
        self._last_new_line -= 1
        self.by_host[host].append(SshConfigLine(
            line=f"  {key} {value}\n",
            line_no=self._last_new_line,
            valid=True,
            host=host,
            key=key,
            value=value,
        ))

    def add_comment(self, host: str | None, text: str) -> None:
        """Add a comment to the host (or at the file start).
        
        The host block must already exist.
        """
        if host not in self.by_host:
            raise SshConfigErr(f"ssh config does not have Host block {host}")
        if not text.lstrip().startswith("#"):
            if host:
                text = "  # " + text
            else:
                text = "# " + text
        self._last_new_line -= 1
        self.by_host[host].append(SshConfigLine(
            line=text + "\n",
            line_no=self._last_new_line,
            valid=True,
            host=host,
            key=None,
            value=None,
        ))

    def add_host(self, host: str) -> None:
        """Add a new host block to the config at the end."""
        if host in self.by_host:
            raise SshConfigErr(f"ssh config already has Host block {host}")
        self._last_new_line -= 1
        self.by_host[host] = [
            SshConfigLine(
                line=f"Host {host}\n",
                line_no=self._last_new_line,
                valid=True,
                host=host,
                key="Host",
                value=host,
            )
        ]

    def get_host_key(self, host: str, key: str) -> str | None:
        """Read the host's key."""
        if host not in self.by_host:
            return None
        key = key.lower()  # keys are case insensitive
        for line in self.by_host[host]:
            if line.key and line.key.lower() == key:
                return line.value
        return None

    def output(self) -> str:
        """Convert the by-host collection into a Host list.
        
        This will correctly interweave the newly added lines into the configuration.
        """
        if not self.by_host:
            return ""
        max_lineno = max([max(n.line_no for n in h) for h in self.by_host.values()])

        def line_order(v: SshConfigLine) -> int:
            if v.line_no <= 0:
                # subtraction -> adding the two together.
                return max_lineno + 1 - v.line_no
            return v.line_no

        host_order = sorted(
            self.by_host.keys(),
            # The host line is always first in the host block.
            # In the case of the None host, it's always first in the file, if it exists.
            key=lambda a: line_order(self.by_host[a][0]),
        )
        ret = ""

        for host in host_order:
            lines = sorted(self.by_host[host], key=line_order)
            ret += "".join([n.line for n in lines])

        return ret

    @staticmethod
    def parse_file(contents: str) -> "SshConfig":
        """Parse a whole ssh_config file."""
        ret: dict[str | None, list[SshConfigLine]] = {}
        host: str | None = None
        line_no = 0
        for line in contents.splitlines(keepends=True):
            line_no += 1
            parsed = SshConfigLine.parse_line(host, line, line_no)
            host = parsed.host
            lines = ret.get(parsed.host)
            if not lines:
                lines = []
                ret[parsed.host] = lines
            lines.append(parsed)
        return SshConfig(ret)

    @staticmethod
    def read_ssh_config(config: UserConfig) -> "SshConfig | None":
        """Read the ssh config and return only if valid."""
        has_error = False
        private_key = config.require_path("ssh", "key-file")
        if not private_key.is_file():
            log.error(
                f"The ssh private key file ({private_key}) does not exist.  "
                f"You probably forgot to run the 'user-setup' sub-command.",
            )
            has_error = True

        ssh_config = SshConfig({})
        ssh_config_file = config.require_path("ssh", "config-file")
        if not ssh_config_file.is_file():
            log.error(
                f"The ssh config file ({ssh_config_file}) does not exist.  "
                f"You probably forgot to run the 'user-setup' sub-command.",
            )
            has_error = True
        else:
            with open(ssh_config_file, "r", encoding="utf-8") as inp:
                ssh_config = SshConfig.parse_file(inp.read())
            format_errs = ssh_config.format_errors()
            if format_errs:
                log.error(f"Thee ssh config file ({ssh_config_file}) has problems:")
                for line in format_errs:
                    log.error(f"  {line.line_no}: {line.line.rstrip()}")
                has_error = True

        if has_error:
            return None
        return ssh_config
