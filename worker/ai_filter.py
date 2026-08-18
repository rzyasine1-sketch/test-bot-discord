"""Swarm 2.0 v3 — WD14 Character & Anime Naming System"""
import re
from typing import Optional, Dict, Set, Tuple
import logging

logger = logging.getLogger("ImageWorker")


class CharacterNamingSystem:
    """Extracts character name and anime title from WD14 (Danbooru) tags."""

    EXCLUDE_CHARACTERS: Set[str] = {
        "original", "oc", "unknown", "multiple_girls", "multiple_boys",
        "1girl", "1boy", "2girls", "2boys", "solo", "duo", "trio"
    }

    ANIME_ALIASES: Dict[str, str] = {
        "re:zero": "Re:Zero",
        "re_zero": "Re:Zero", 
        "rezero": "Re:Zero",
        "konosuba": "KonoSuba",
        "kono_subarashii_sekai_ni_shukufuku_wo": "KonoSuba",
        "gochiusa": "GochiUsa",
        "gochuumon_wa_usagi_desu_ka": "GochiUsa",
        "monogatari": "Monogatari",
        "bakemonogatari": "Monogatari",
        "fate": "Fate",
        "fate/grand_order": "Fate/GO",
        "fgo": "Fate/GO",
        "love_live": "Love Live!",
        "love_live!": "Love Live!",
        "k-on": "K-On!",
        "k_on": "K-On!",
        "steins;gate": "Steins;Gate",
        "steins_gate": "Steins;Gate",
        "attack_on_titan": "Attack on Titan",
        "shingeki_no_kyojin": "Attack on Titan",
        "one_piece": "One Piece",
        "naruto": "Naruto",
        "dragon_ball": "Dragon Ball",
        "demon_slayer": "Demon Slayer",
        "kimetsu_no_yaiba": "Demon Slayer",
        "jujutsu_kaisen": "Jujutsu Kaisen",
        "my_hero_academia": "My Hero Academia",
        "boku_no_hero_academia": "My Hero Academia",
        "spy_x_family": "Spy x Family",
        "chainsaw_man": "Chainsaw Man",
        "blue_lock": "Blue Lock",
        "oshinoko": "Oshi no Ko",
        "idolmaster": "THE iDOLM@STER",
        "the_idolmaster": "THE iDOLM@STER",
        "hololive": "Hololive",
        "gawr_gura": "Hololive",
        "hatsune_miku": "Vocaloid",
        "vocaloid": "Vocaloid",
    }

    @classmethod
    def parse_character_and_anime(cls, tags_str: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not tags_str:
            return None, None

        tags = [t.strip().lower() for t in tags_str.replace("_", " ").split()]
        character = None
        anime = None

        # Pattern: character_name_(anime_name) — using re.escape for safety
        char_pattern = re.compile(r"^([a-z0-9-]+)_\(([a-z0-9-:]+)\)$")

        for tag in tags:
            match = char_pattern.match(tag)
            if match:
                char_candidate = match.group(1).replace("-", " ").title()
                anime_candidate = match.group(2)
                if char_candidate.lower() not in cls.EXCLUDE_CHARACTERS:
                    character = char_candidate
                    anime = cls._normalize_anime(anime_candidate)
                    break

        # Search copyright/series tags if no anime found
        if not anime:
            for tag in tags:
                if tag.startswith("copyright "):
                    anime_raw = tag.replace("copyright ", "")
                    anime = cls._normalize_anime(anime_raw)
                    break
                series_pattern = re.compile(r"^(series|from|copyright)_(.+)$")
                match = series_pattern.match(tag)
                if match:
                    anime = cls._normalize_anime(match.group(2))
                    break

        # If character found but no anime, search for any anime-like tag
        if character and not anime:
            for tag in tags:
                normalized = cls._normalize_anime(tag)
                if normalized and normalized != tag:
                    anime = normalized
                    break

        return character, anime

    @classmethod
    def _normalize_anime(cls, raw: str) -> Optional[str]:
        if not raw:
            return None

        raw_clean = raw.lower().replace("-", "_").replace(" ", "_")

        if raw_clean in cls.ANIME_ALIASES:
            return cls.ANIME_ALIASES[raw_clean]

        for prefix in ["series_", "from_", "copyright_"]:
            if raw_clean.startswith(prefix):
                raw_clean = raw_clean[len(prefix):]
                if raw_clean in cls.ANIME_ALIASES:
                    return cls.ANIME_ALIASES[raw_clean]

        return raw.replace("_", " ").replace("-", " ").title()

    @classmethod
    def generate_display_name(cls, character, anime, subcategory, image_id):
        item_type = "Avatar" if subcategory in ("anime_girls", "anime_boys", "anime_couples") else "Banner"

        if character and anime:
            return f"{character} ({anime}) - {item_type} #{image_id}"
        elif character:
            return f"{character} - {item_type} #{image_id}"
        elif anime:
            return f"{anime} Character - {item_type} #{image_id}"
        else:
            category_label = subcategory.replace("_", " ").title()
            return f"{category_label} - {item_type} #{image_id}"

    @classmethod
    def extract_from_tags(cls, tags_str, subcategory, image_id):
        character, anime = cls.parse_character_and_anime(tags_str)
        display = cls.generate_display_name(character, anime, subcategory, image_id)

        return {
            "character_name": character or "",
            "anime_name": anime or "",
            "display_name": display
        }


class WD14Classifier:
    """Backward-compatible WD14 filter used by the validator.

    This keeps the older worker API working while the newer character-naming
    helpers remain available in the same module.
    """

    def __init__(self):
        self.logger = logging.getLogger("ImageWorker")

    def classify(self, tags: Optional[str]) -> Dict[str, object]:
        tag_text = tags or ""
        normalized = set(tag_text.lower().replace("_", " ").split())

        is_nsfw = any(token in normalized for token in [
            "nsfw", "explicit", "sexy", "hentai", "porn", "xxx",
            "nude", "r18", "loli", "shota"
        ])
        is_loli = any(token in normalized for token in [
            "loli", "child", "young", "shota"
        ])

        if any(token in normalized for token in ["1girl", "girl", "girls", "female", "waifu"]):
            gender = "girls"
        elif any(token in normalized for token in ["1boy", "boy", "boys", "male", "husbando"]):
            gender = "boys"
        elif any(token in normalized for token in ["couple", "couples", "group", "multiple_girls", "multiple_boys"]):
            gender = "couples_or_group"
        else:
            gender = "unknown"

        if is_nsfw or is_loli:
            reason = "nsfw_or_loli_tags"
            is_safe = False
        else:
            reason = "safe"
            is_safe = True

        return {
            "is_safe": is_safe,
            "reason": reason,
            "is_nsfw": bool(is_nsfw),
            "is_loli": bool(is_loli),
            "gender": gender,
        }
