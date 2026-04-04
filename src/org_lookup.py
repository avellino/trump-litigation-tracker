"""Organization name normalization for attorney data."""

import re
from typing import Optional

# Organization normalization patterns
# Keys are regex patterns (lowercase), values are normalized names
ORG_PATTERNS = [
    # ACLU variations
    (r"aclu.*foundation", "ACLU"),
    (r"american\s+civil\s+liberties\s+union", "ACLU"),
    (r"aclu\s+immigrants?\s+rights", "ACLU - Immigrants' Rights Project"),
    (r"aclu\s+reproductive\s+rights", "ACLU - Reproductive Justice"),
    (r"aclu\s+voting\s+rights", "ACLU - Voting Rights Project"),
    (r"\baclu\b", "ACLU"),

    # DOJ variations
    (r"doj\s*-?\s*civ.*immigration", "DOJ - Immigration Litigation"),
    (r"doj\s*-?\s*civ.*enforcement", "DOJ - Enforcement Litigation"),
    (r"department\s+of\s+justice.*civil\s+division", "DOJ - Civil Division"),
    (r"department\s+of\s+justice.*environment", "DOJ - Environment & Natural Resources"),
    (r"department\s+of\s+justice.*tax", "DOJ - Tax Division"),
    (r"department\s+of\s+justice.*antitrust", "DOJ - Antitrust Division"),
    (r"u\.?\s*s\.?\s+department\s+of\s+justice", "DOJ"),
    (r"^\s*doj\s*$", "DOJ"),
    (r"^doj\b", "DOJ"),
    (r"\bu\s+s\s+department\s+of\s+justice\b", "DOJ"),

    # Department of Homeland Security
    (r"department\s+of\s+homeland\s+security", "DHS"),
    (r"dhs\s*:", "DHS"),
    (r"\sdhs\s*$", "DHS"),

    # Department of State
    (r"department\s+of\s+state", "Department of State"),

    # US Attorneys (by district)
    (r"u\.?\s*s\.?\s+attorney.*edny", "USAO - E.D.N.Y."),
    (r"u\.?\s*s\.?\s+attorney.*sdny", "USAO - S.D.N.Y."),
    (r"u\.?\s*s\.?\s+attorney.*ndca", "USAO - N.D. Cal."),
    (r"u\.?\s*s\.?\s+attorney.*cdca", "USAO - C.D. Cal."),
    (r"u\.?\s*s\.?\s+attorney.*ddc", "USAO - D.D.C."),
    (r"u\.?\s*s\.?\s+attorney.*mdfl", "USAO - M.D. Fla."),
    (r"u\.?\s*s\.?\s+attorney.*tx", "USAO - Texas"),
    (r"u\.?\s*s\.?\s+attorney\s+for\s+the\s+district", "US Attorney's Office"),

    # State Attorneys General
    (r"new\s+york\s+(state\s+)?attorney\s+general|letticia\s+james|letitia\s+james", "NY AG"),
    (r"california\s+(state\s+)?attorney\s+general|rob\s+bonta|robert\s+bonta", "CA AG"),
    (r"illinois\s+(state\s+)?attorney\s+general|kirk\s+werner|kuihn", "IL AG"),
    (r"massachusetts\s+(state\s+)?attorney\s+general|maura\s+healey", "MA AG"),
    (r"washington\s+(state\s+)?attorney\s+general|bob\s+fellows", "WA AG"),
    (r"pennsylvania\s+(state\s+)?attorney\s+general|koscielak", "PA AG"),
    (r"colorado\s+(state\s+)?attorney\s+general|weinstein", "CO AG"),
    (r"michigan\s+(state\s+)?attorney\s+general|david\s+rissman", "MI AG"),
    (r"minnesota\s+(state\s+)?attorney\s+general|keith\s+ellison", "MN AG"),
    (r"virginia\s+(state\s+)?attorney\s+general|jerry\s+hickenlooper", "VA AG"),
    (r"ohio\s+(state\s+)?attorney\s+general|david\s+yost", "OH AG"),
    (r"oregon\s+(state\s+)?attorney\s+general|ellen\s+fuentes", "OR AG"),
    (r"hawaii\s+(state\s+)?attorney\s+general", "HI AG"),
    (r"vermont\s+(state\s+)?attorney\s+general", "VT AG"),
    (r"delaware\s+(state\s+)?attorney\s+general", "DE AG"),
    (r"connecticut\s+(state\s+)?attorney\s+general", "CT AG"),
    (r"state\s+of\s+(new\s+york|california|illinois|massachusetts|washington|pennsylvania|colorado|virginia|michigan|minnesota|ohio|oregon|hawaii|vermont|delaware|connecticut)", "State AG Coalition"),

    # States as parties
    (r"^state\s+of\s+new\s+york$", "State of New York"),
    (r"^state\s+of\s+california$", "State of California"),
    (r"^state\s+of\s+illinois$", "State of Illinois"),
    (r"^state\s+of\s+massachusetts$", "State of Massachusetts"),
    (r"^state\s+of\s+washington$", "State of Washington"),
    (r"^state\s+of\s+pennsylvania$", "State of Pennsylvania"),
    (r"^state\s+of\s+colorado$", "State of Colorado"),
    (r"^state\s+of\s+virginia$", "State of Virginia"),
    (r"^state\s+of\s+michigan$", "State of Michigan"),
    (r"^state\s+of\s+minnesota$", "State of Minnesota"),
    (r"^state\s+of\s+ohio$", "State of Ohio"),
    (r"^state\s+of\s+oregon$", "State of Oregon"),
    (r"^state\s+of\s+hawaii$", "State of Hawaii"),
    (r"^state\s+of\s+vermont$", "State of Vermont"),
    (r"^state\s+of\s+delaware$", "State of Delaware"),
    (r"^state\s+of\s+connecticut$", "State of Connecticut"),
    (r"^state\s+of\s+maryland$", "State of Maryland"),
    (r"^state\s+of\s+north\s+carolina$", "State of North Carolina"),
    (r"^state\s+of\s+d.c\.$", "District of Columbia"),
    (r"^district\s+of\s+columbia$", "District of Columbia"),
    (r"^dc$", "District of Columbia"),

    # Major law firms (will expand based on actual data)
    (r"goodwin\s+procter", "Goodwin Procter"),
    (r"covington\s+&?\s*burling", "Covington & Burling"),
    (r"kaye\s+scholer", "Kaye Scholer"),
    (r"hogan\s+lovells", "Hogan Lovells"),
    (r"williams\s+jenkins", "Williams & Jenkins"),
    (r"paul,\s*hastings", "Paul Hastings"),
    (r"gibson\s+dunn", "Gibson Dunn"),
    (r"skadden", "Skadden Arps"),
    (r"latham\s+&\s+watkins", "Latham & Watkins"),
    (r"sullivan\s+cromwell", "Sullivan & Cromwell"),

    # Civil rights and advocacy orgs
    (r"center\s+for\s+american\s+progress", "CAP"),
    (r"american-assoc\s+for\s+justice", "American Association for Justice"),
    (r"national\s+association\s+for\s+the\s+advancement", "NAACP"),
    (r"aclu\s*:", "ACLU"),
    (r"\bnational\s+immigration\s+lawyers\s+association", "NILA"),
    (r"human\s+rights\s+watch", "Human Rights Watch"),
    (r"american\s+bar\s+association", "ABA"),
    (r"national\s+legal\s+aid\s+and\s+defenders\s+fund", "NLADF"),
    (r"church\s+of\s+jesus\s+christ", "Church of Jesus Christ"),
    (r"jewish\s+defence\s+league", "Jewish Defence League"),
    (r"american-civil\s+liberties", "ACLU"),
    (r"immigrant\s+rights\s+legal\s+center", "IRLC"),

    # Media orgs
    (r"lawfare", "Lawfare Institute"),
    (r"just\s+security", "Just Security"),
    (r"new\s+york\s+times", "New York Times"),
    (r"propublica", "ProPublica"),
    (r"the\s+hil", "The Hill"),
]


def normalize_organization(org: str) -> str:
    """
    Normalize an organization name using the pattern lookup.

    Args:
        org: Raw organization name from court records

    Returns:
        Normalized organization name, or original if no match found
    """
    if not org:
        return "Unknown"

    # Convert to lowercase for matching
    org_lower = org.lower().strip()

    # Try each pattern
    for pattern, normalized in ORG_PATTERNS:
        if re.search(pattern, org_lower):
            return normalized

    # If no match, clean up the original
    # Remove extra whitespace and normalize
    cleaned = re.sub(r"\s+", " ", org.strip())
    return cleaned if cleaned else "Unknown"
