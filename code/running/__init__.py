"""RURFFL Robo Owner - AI franchise manager for the R U Ready 4 Some Football?! league."""

# urllib3/chardet version mismatch warns on every single import. Silencing it here
# rather than piping every command through grep: grep buffers when it is not writing
# to a terminal, which made a 20-minute background run look completely dead.
import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*urllib3.*chardet.*")
_warnings.filterwarnings("ignore", category=Warning, module="requests")

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"

LEAGUE_ID_2026 = "1383503237683879936"
LEAGUE_ID_2025 = "1255710645953773568"
LEAGUE_ID_2024 = "1124837824776925184"
DRAFT_ID_2026 = "1383503237696454656"
DRAFT_ID_2025 = "1255710645957967872"
DRAFT_ID_2024 = "1124837824776925185"
# The public site, in ONE place. This URL is quoted in the bot's persona, its
# GroupMe avatar, the README and the docs, and the bot has already posted it to
# GroupMe -- where posts cannot be deleted. Six hardcoded copies is how a rename
# leaves a dead link somewhere permanent, so renaming the repo is a one-word
# change here and nowhere else.
SITE_OWNER = "Anders0nAZ"
SITE_REPO = "robowner"
SITE_URL = f"https://{SITE_OWNER.lower()}.github.io/{SITE_REPO}/"
AVATAR_URL = f"https://raw.githubusercontent.com/{SITE_OWNER}/{SITE_REPO}/main/avatar.png"

# The franchise name as it stands on Sleeper. Inherited as "Morris' Mafia"
# and renamed; the public decision log said the old one for a day after the
# rename because it was hardcoded into the renderer. Live value lives in
# league_user metadata (sleeper_write.set_team_name).
TEAM_NAME = "Techanical Merc"

ROBOWNER_USER_ID = "1397683353888530432"
TAKEN_OVER_FROM = "SinfonianPoke"  # Morris' Mafia, 2025 roster_id 4, lottery slot 7
ROSTER_ID = 4  # carried over franchise roster id in 2025 league; confirm same in 2026 once rosters exist
