import datetime
import uuid
import json # Import json for category handling
from typing import Any, Optional, Dict, Union
from urllib.parse import urlparse # Import urlparse

import feedparser
import feedgen.feed
import validators # Import validators
from feedparser.util import FeedParserDict
from flask import url_for

from app import config, db, logger
from app.models import Feed, Post
from shared.podcast_downloader import find_audio_link


# --- Helper functions for safe data extraction ---

def _get_feed_attr(feed_data: FeedParserDict, key: str, default: Any = None) -> Any:
    """Safely get an attribute from the feed's root level."""
    return feed_data.feed.get(key, default)

def _get_entry_attr(entry: FeedParserDict, key: str, default: Any = None) -> Any:
    """Safely get an attribute from a feed entry."""
    return entry.get(key, default)

def _get_itunes_explicit(data: Dict[str, Any]) -> Optional[bool]:
    """Parse itunes:explicit value ('yes', 'no', 'true', 'false', 'clean') into boolean.
       Defaults to False if tag is present but unrecognized, None if absent.
    """
    explicit_val = data.get("itunes_explicit") # Get raw value
    if explicit_val is None:
        return None # Tag not present

    explicit_str = str(explicit_val).lower().strip()
    if explicit_str in ["yes", "true"]:
        return True
    # Treat 'no', 'false', 'clean', or any other non-empty value that isn't 'yes'/'true' as False
    if explicit_str:
        return False 
    # Return None only if the tag was present but completely empty? Or default to false?
    # Let's default to False if present but empty/unrecognized for safety.
    return False


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    logger.info(f"Fetching feed from URL: {url}")
    feed_data = feedparser.parse(url)
    for entry in feed_data.entries:
        entry.id = get_guid(entry)
    return feed_data


def refresh_feed(feed: Feed) -> None:
    logger.info(f"Refreshing feed with ID: {feed.id}")
    feed_data = fetch_feed(feed.rss_url)
    updated = False

    # Update basic feed info if changed
    new_image_url = _get_feed_attr(feed_data, "image", {}).get("href")
    if new_image_url and feed.image_url != new_image_url:
        feed.image_url = new_image_url
        updated = True

    new_title = _get_feed_attr(feed_data, "title")
    if new_title and feed.title != new_title:
        feed.title = new_title
        updated = True

    new_desc = _get_feed_attr(feed_data, "description")
    if new_desc and feed.description != new_desc:
        feed.description = new_desc
        updated = True

    new_author = _get_feed_attr(feed_data, "author")
    if new_author and feed.author != new_author:
        feed.author = new_author
        updated = True

    # Update language and iTunes info
    new_lang = _get_feed_attr(feed_data, "language")
    if new_lang and feed.language != new_lang:
        feed.language = new_lang
        updated = True

    new_itunes_author = _get_feed_attr(feed_data, "itunes_author")
    if new_itunes_author and feed.itunes_author != new_itunes_author:
        feed.itunes_author = new_itunes_author
        updated = True

    new_itunes_subtitle = _get_feed_attr(feed_data, "itunes_subtitle")
    if new_itunes_subtitle and feed.itunes_subtitle != new_itunes_subtitle:
        feed.itunes_subtitle = new_itunes_subtitle
        updated = True

    new_itunes_summary = _get_feed_attr(feed_data, "itunes_summary")
    if new_itunes_summary and feed.itunes_summary != new_itunes_summary:
        feed.itunes_summary = new_itunes_summary
        updated = True

    new_itunes_type = _get_feed_attr(feed_data, "itunes_type")
    if new_itunes_type and feed.itunes_type != new_itunes_type:
        feed.itunes_type = new_itunes_type
        updated = True

    new_itunes_explicit = _get_itunes_explicit(feed_data.feed)
    if new_itunes_explicit is not None and feed.itunes_explicit != new_itunes_explicit:
        feed.itunes_explicit = new_itunes_explicit
        updated = True

    owner = _get_feed_attr(feed_data, "itunes_owner", {})
    new_owner_name = owner.get("name")
    new_owner_email = owner.get("email")
    if new_owner_name and feed.itunes_owner_name != new_owner_name:
        feed.itunes_owner_name = new_owner_name
        updated = True
    if new_owner_email and feed.itunes_owner_email != new_owner_email:
        feed.itunes_owner_email = new_owner_email
        updated = True

    # Update keywords and categories
    new_keywords = _get_feed_attr(feed_data, "itunes_keywords")
    if new_keywords and feed.itunes_keywords != new_keywords:
        feed.itunes_keywords = new_keywords
        updated = True

    # feedparser stores categories in feed_data.feed.tags as a list of dicts
    # [{'term': 'News', 'scheme': '...', 'label': None}, ...]
    new_categories = _get_feed_attr(feed_data, "tags")
    if new_categories:
        new_categories_json = json.dumps(new_categories)
        if feed.itunes_categories_json != new_categories_json:
            feed.itunes_categories_json = new_categories_json
            updated = True

    if updated:
        db.session.add(feed)
        # Commit potentially later if also adding posts

    # Add new posts
    existing_posts = {post.guid for post in feed.posts} # type: ignore[attr-defined]
    oldest_post = min(
        (post for post in feed.posts if post.release_date), # type: ignore[attr-defined]
        key=lambda p: p.release_date,
        default=None,
    )
    posts_added = False
    for entry in feed_data.entries:
        if entry.id not in existing_posts: # entry.id is our calculated GUID here
            logger.debug(f"found new podcast: {entry.title}")
            p = make_post(feed, entry)
            # do not allow automatic download of any backcatalog added to the feed
            if (
                oldest_post is not None
                and p.release_date is not None # Check p.release_date exists
                and oldest_post.release_date is not None # Check oldest_post.release_date exists
                and p.release_date.date() < oldest_post.release_date.date() # Compare dates only
            ):
                p.whitelisted = False
                logger.debug(
                    f"skipping post from archive due to \
number_of_episodes_to_whitelist_from_archive_of_new_feed setting: {entry.title}"
                )
            else:
                p.whitelisted = config.automatically_whitelist_new_episodes
            db.session.add(p)
            posts_added = True

    if updated or posts_added:
        db.session.commit()
        logger.info(f"Feed with ID: {feed.id} refreshed (updated: {updated}, posts added: {posts_added})")
    else:
        logger.info(f"Feed with ID: {feed.id} already up-to-date.")


def add_or_refresh_feed(url: str) -> Feed:
    feed_data = fetch_feed(url)
    if "title" not in feed_data.feed:
        logger.error("Invalid feed URL")
        raise ValueError(f"Invalid feed URL: {url}")

    feed = Feed.query.filter_by(rss_url=url).first()
    if feed:
        refresh_feed(feed)
    else:
        feed = add_feed(feed_data)
    return feed # type: ignore[no-any-return]


def add_feed(feed_data: feedparser.FeedParserDict) -> Feed:
    logger.info(f"Storing feed: {feed_data.feed.title}")
    # --- DEBUGGING: Log available keys (Remove) ---
    # logger.debug(f"feed_data.feed keys: {list(feed_data.feed.keys())}")
    # if feed_data.entries:
    #        logger.debug(f"First entry keys: {list(feed_data.entries[0].keys())}")
    # --- END DEBUGGING --- 
    try:
        # Extract feed-level data using available keys
        # owner = _get_feed_attr(feed_data, "itunes_owner", {}) # Not available via feedparser
        categories = _get_feed_attr(feed_data, "tags")
        categories_json = json.dumps(categories) if categories else None
        std_author = _get_feed_attr(feed_data, "author")
        std_summary = _get_feed_attr(feed_data, "summary", _get_feed_attr(feed_data, "description", ""))

        feed = Feed(
            title=_get_feed_attr(feed_data, "title", "Untitled Feed"),
            description=_get_feed_attr(feed_data, "description", ""),
            author=std_author,
            rss_url=feed_data.href, 
            image_url=_get_feed_attr(feed_data, "image", {}).get("href"),
            language=_get_feed_attr(feed_data, "language"),
            # Use standard author if itunes:author not parsed by feedparser
            itunes_author=std_author, 
            # Use feed subtitle if itunes:subtitle not parsed by feedparser
            itunes_subtitle=_get_feed_attr(feed_data, "subtitle"), 
            # Use standard summary if itunes:summary not parsed
            itunes_summary=std_summary, 
            # Pass feed_data.feed directly to helper
            itunes_explicit=_get_itunes_explicit(feed_data.feed),
            itunes_type=None, # Not available via feedparser
            itunes_owner_name=None, # Not available via feedparser
            itunes_owner_email=None, # Not available via feedparser
            itunes_keywords=None, # Not available via feedparser
            itunes_categories_json=categories_json,
        )
        db.session.add(feed)
        db.session.commit() # Commit feed first to get feed.id

        num_posts_added = 0
        for entry in feed_data.entries:
            p = make_post(feed, entry)
            if (
                config.number_of_episodes_to_whitelist_from_archive_of_new_feed
                is not None
                and num_posts_added
                >= config.number_of_episodes_to_whitelist_from_archive_of_new_feed
            ):
                logger.info(
                    f"Number of episodes to load from archive reached: {num_posts_added}"
                )
                p.whitelisted = False
            else:
                num_posts_added += 1
                p.whitelisted = config.automatically_whitelist_new_episodes
            db.session.add(p)
        db.session.commit() # Commit posts
        logger.info(f"Feed stored with ID: {feed.id}")
        return feed
    except Exception as e:
        logger.error(f"Failed to store feed: {e}", exc_info=True) # Log traceback
        db.session.rollback()
        raise e


def format_duration(seconds: Optional[int]) -> Optional[str]:
    """Formats seconds into HH:MM:SS string, returns None if input is None or invalid."""
    if seconds is None or seconds < 0:
        return None
    try:
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    except (ValueError, TypeError):
        return None


def generate_feed_xml(feed: Feed) -> str:
    """Generates the podcast RSS feed XML using the feedgen library."""
    logger.info(f"Generating feedgen XML for feed with ID: {feed.id}")

    fg = feedgen.feed.FeedGenerator()
    fg.load_extension('podcast') # Load the iTunes/Podcast extension

    # --- Feed Metadata ---
    feed_link = url_for("main.get_feed", f_id=feed.id, _external=True)
    fg.title("[podly] " + feed.title)
    fg.link(href=feed_link, rel='alternate') # Link to the feed itself
    fg.description(feed.description or feed.itunes_summary or '') # Use description or itunes summary
    fg.language(feed.language or 'en') # Default to 'en' if not set
    if feed.image_url:
        fg.image(url=feed.image_url, title=feed.title, link=feed_link)
    fg.lastBuildDate(datetime.datetime.now(datetime.timezone.utc)) # Use timezone-aware datetime
    fg.author({'name': feed.author or feed.itunes_author or 'Podly'}) # Use author or itunes author

    # --- iTunes Feed Metadata ---
    # Use getattr for safety, although fields exist now
    fg.podcast.itunes_author(getattr(feed, 'itunes_author', feed.author))
    fg.podcast.itunes_subtitle(getattr(feed, 'itunes_subtitle', ''))
    fg.podcast.itunes_summary(getattr(feed, 'itunes_summary', feed.description))

    # Apply image check/hack logic to channel image
    channel_image_url = getattr(feed, 'image_url', None)
    if channel_image_url:
        try:
            # First, check if the ORIGINAL url already satisfies feedgen
            if channel_image_url.lower().endswith(('.png', '.jpg')):
                 # logger.debug(f"Channel image URL '{channel_image_url}' ends correctly. Using directly.") # Remove debug
                 fg.podcast.itunes_image(channel_image_url)
            else:
                # Original URL failed validation, try the hack
                parsed_url = urlparse(channel_image_url)
                separator = '&' if parsed_url.query else '?'
                hacked_url = channel_image_url + separator + 'feedgen=.jpg'
                logger.warning(
                    f"Channel image URL '{channel_image_url}' does not end with .png or .jpg. "
                    f"Attempting hack: '{hacked_url}' for feed {feed.id}"
                )
                try:
                   fg.podcast.itunes_image(hacked_url)
                except ValueError:
                     logger.error(f"Hack failed for channel image URL '{channel_image_url}'. Skipping itunes:image for feed {feed.id}.")
        except Exception as e:
             # logger.warning(...) # Keep warning
            pass # Keep exception handling

    # Handle explicit tag generation (check if value is True/False)
    explicit_val = getattr(feed, 'itunes_explicit', None)
    # logger.debug(f"Channel explicit value from DB: {explicit_val} (Type: {type(explicit_val)})") # Remove debug
    if explicit_val is not None:
        explicit_str = 'yes' if explicit_val else 'no'
        # logger.debug(f"Setting channel itunes:explicit to: {explicit_str}") # Remove debug
        fg.podcast.itunes_explicit(explicit_str)
    fg.podcast.itunes_owner(name=getattr(feed, 'itunes_owner_name', None),
                             email=getattr(feed, 'itunes_owner_email', None))
    if getattr(feed, 'itunes_type', None):
        fg.podcast.itunes_type(feed.itunes_type)

    # Add keywords as standard RSS categories
    keywords_str = getattr(feed, 'itunes_keywords', None)
    if keywords_str:
        keywords_list = [k.strip() for k in keywords_str.split(',') if k.strip()]
        for keyword in keywords_list:
            fg.category(term=keyword)

    # Add iTunes categories from stored JSON
    if getattr(feed, 'itunes_categories_json', None):
        try:
            categories = json.loads(feed.itunes_categories_json)
            if isinstance(categories, list):
                processed_cats = set()
                for cat_data in categories:
                    # Basic check for valid structure and non-empty term
                    if isinstance(cat_data, dict) and cat_data.get('term'):
                        term = cat_data['term']
                        # Explicitly add known valid iTunes categories
                        if term in ["News", "Daily News"] and term not in processed_cats:
                            # logger.debug(f"Adding valid iTunes category: {term}") # Remove debug
                            fg.podcast.itunes_category(term)
                            processed_cats.add(term)
                        elif term not in processed_cats:
                            # Log other terms found but don't add as itunes:category
                            # logger.debug(f"Skipping non-standard term for itunes:category: {term}") # Remove debug
                            processed_cats.add(term) # Still mark as processed
        except json.JSONDecodeError:
             # logger.warning(...) # Keep warning
             pass # Keep exception handling

    # --- Feed Items (Posts) ---
    server_prefix = config.server if config.server is not None else ""
    for post in sorted(feed.posts, key=lambda p: p.release_date or datetime.datetime.min, reverse=True): # type: ignore[attr-defined, misc]
        fe = fg.add_entry() # Create a feed entry

        post_details_url = server_prefix + url_for(
            "main.post_page",
            p_guid=post.guid,
            _external=config.server is None,
        )
        podly_audio_url = server_prefix + url_for(
            "main.download_post",
            p_guid=post.guid,
            _external=config.server is None,
        )

        fe.guid(post.guid, permalink=False) # GUID is crucial
        fe.title(post.title or "Untitled Post")
        fe.link(href=post_details_url) # Link to the Podly post page

        # Description: Use post.description, append Podly link
        description_html = f'{post.description or post.itunes_summary or ""}\n<p><a href="{post_details_url}">Podly Post Page</a></p>'
        fe.description(description_html, isSummary=False)

        if post.release_date:
             # Ensure datetime is timezone-aware (assume UTC if not specified)
            release_date_aware = post.release_date
            if isinstance(release_date_aware, datetime.datetime) and release_date_aware.tzinfo is None:
                release_date_aware = release_date_aware.replace(tzinfo=datetime.timezone.utc)
            fe.pubDate(release_date_aware)

        # Enclosure (Audio File)
        fe.enclosure(
            url=podly_audio_url,
            length=str(post.audio_len_bytes()), # Length must be string
            type="audio/mpeg", # Assuming mp3, adjust if needed
        )

        # --- iTunes Item Metadata ---
        # Format duration manually
        duration_str = format_duration(getattr(post, 'duration', None))
        if duration_str:
             # Use the podcast extension's internal setter directly if possible,
             # otherwise, we might need to add the element manually.
             # Let's try setting it via the main property first.
             try:
                fe.podcast.itunes_duration(duration_str)
             except Exception:
                 # Fallback or log if direct setting fails
                 logger.warning(f"Could not set formatted duration '{duration_str}' via feedgen method. Tag may be missing.")

        fe.podcast.itunes_subtitle(getattr(post, 'itunes_subtitle', ''))
        fe.podcast.itunes_summary(getattr(post, 'itunes_summary', post.description))
        # Handle explicit tag generation (check if value is True/False)
        item_explicit_val = getattr(post, 'itunes_explicit', None)
        # logger.debug(f"Item {post.id} explicit value from DB: {item_explicit_val} (Type: {type(item_explicit_val)})") # Remove debug
        if item_explicit_val is not None:
            item_explicit_str = 'yes' if item_explicit_val else 'no'
            # logger.debug(f"Setting item {post.id} itunes:explicit to: {item_explicit_str}") # Remove debug
            fe.podcast.itunes_explicit(item_explicit_str)
        fe.podcast.itunes_episode_type(getattr(post, 'itunes_episode_type', None))

        # Apply image check/hack logic to item image
        episode_image_url = getattr(post, 'itunes_image_url', None)
        if episode_image_url:
            try:
                # First, check if the ORIGINAL url already satisfies feedgen
                if episode_image_url.lower().endswith(('.png', '.jpg')):
                     # logger.debug(f"Episode image URL '{episode_image_url}' ends correctly. Using directly for post {post.id}.") # Remove debug
                     fe.podcast.itunes_image(episode_image_url)
                else:
                    # Original URL failed validation, try the hack
                    parsed_url = urlparse(episode_image_url)
                    separator = '&' if parsed_url.query else '?'
                    hacked_url = episode_image_url + separator + 'feedgen=.jpg'
                    logger.warning(
                        f"Episode image URL '{episode_image_url}' does not end with .png or .jpg. "
                        f"Attempting hack: '{hacked_url}' for post {post.id}"
                    )
                    try:
                       fe.podcast.itunes_image(hacked_url)
                    except ValueError:
                         logger.error(f"Hack failed for image URL '{episode_image_url}'. Skipping itunes:image for post {post.id}.")
            except Exception as e:
                 # logger.warning(...) # Keep warning
                 pass # Keep exception handling

    logger.info(f"Feedgen XML generated for feed with ID: {feed.id}")
    return fg.rss_str(pretty=True) # Generate the RSS XML string


def make_post(feed: Feed, entry: feedparser.FeedParserDict) -> Post:
    # Use helpers to extract entry data
    description = _get_entry_attr(entry, "description", "")
    # Use standard summary if itunes:summary not available in entry keys
    summary = _get_entry_attr(entry, "summary", description) 
    # Use standard summary if itunes:summary not available
    itunes_summary = summary 
    # itunes_subtitle not available in entry keys
    itunes_subtitle = None 

    # Parse release date
    published_parsed = _get_entry_attr(entry, "published_parsed")
    release_date = None
    if published_parsed:
        try:
            # feedparser gives time.struct_time, convert to datetime
            release_date = datetime.datetime(*published_parsed[:6])
        except (TypeError, ValueError):
            logger.warning(f"Could not parse release date for post: {entry.get('title', 'N/A')}")

    # Extract episode-specific iTunes fields
    episode_type = _get_entry_attr(entry, "itunes_episodetype")
    image_url = _get_entry_attr(entry, "itunes_image")
    if not image_url:
        image_data = _get_entry_attr(entry, "image")
        if isinstance(image_data, dict):
            image_url = image_data.get("href")

    return Post(
        feed_id=feed.id,
        guid=entry.id, # Use the pre-calculated GUID
        download_url=find_audio_link(entry), # Assuming this finds the right URL
        title=_get_entry_attr(entry, "title", "Untitled Post"),
        description=description,
        release_date=release_date,
        duration=get_duration(entry),
        itunes_subtitle=itunes_subtitle, # Will be None
        itunes_summary=itunes_summary,
        # Pass entry directly to helper
        itunes_explicit=_get_itunes_explicit(entry),
        itunes_episode_type=episode_type,
        itunes_image_url=image_url,
    )


# sometimes feed entry ids are the post url or something else
def get_guid(entry: feedparser.FeedParserDict) -> str:
    # Check for guid field first, as it's preferred
    guid = _get_entry_attr(entry, 'guid')
    # ONLY use it if it's present AND explicitly marked as NOT a permalink
    if guid and _get_entry_attr(entry, 'guid_ispermalink') is False:
        # Still double-check it doesn't look like a URL, just in case
        if not validators.url(str(guid)):
             return str(guid)

    # Check for atom id
    atom_id = _get_entry_attr(entry, 'id')
    if atom_id:
        # Check if atom_id is a URL. If so, ignore it.
        if not validators.url(str(atom_id)):
            return str(atom_id)

    # Fallback to feedparser's default calculated 'id' field
    # Check if feedparser_id is a URL. If so, ignore it.
    try:
        feedparser_id = entry.id # Access directly as it's calculated in fetch_feed
        if feedparser_id and not validators.url(str(feedparser_id)):
            # If it's not a URL, maybe it's a UUID?
            try:
                uuid.UUID(str(feedparser_id))
                return str(feedparser_id)
            except (ValueError, TypeError):
                 # Not a valid UUID, but also not a URL. Could still be a usable ID?
                 # Let's risk using it if it's not overly long (potential hash?)
                 # This is debatable, hashing might be safer.
                 # For now, let's prefer hashing if it's not a UUID.
                 pass # Fall through to hashing

    except (AttributeError, KeyError):
        pass # entry.id not present

    # --- Fallbacks --- 
    # Hashing the download URL is usually the most reliable fallback
    dlurl = find_audio_link(entry)
    if dlurl:
        # Ensure we use a consistent hash
        return str(uuid.uuid5(uuid.NAMESPACE_URL, dlurl))

    # Absolute fallback: hash the title + date? Less ideal due to potential changes
    logger.warning(f"Could not find stable GUID (guid/id not URL, no dl_url) for entry: {entry.get('title', 'N/A')}. Hashing title+date.")
    title_str = entry.get('title', '')
    date_str = str(entry.get('published_parsed', ''))
    # Use a different namespace maybe? Or just URL namespace is fine.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, title_str + date_str))


def get_duration(entry: feedparser.FeedParserDict) -> Optional[int]:
    """Attempts to parse duration (iTunes preferred) into seconds."""
    duration_str = _get_entry_attr(entry, "itunes_duration")
    if duration_str:
        try:
            parts = list(map(int, duration_str.split(':')))
            if len(parts) == 3: # HH:MM:SS
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2: # MM:SS
                return parts[0] * 60 + parts[1]
            elif len(parts) == 1: # Seconds
                return parts[0]
        except (ValueError, TypeError):
            logger.warning(f"Could not parse itunes:duration '{duration_str}' for post: {entry.get('title', 'N/A')}")

    # Maybe add fallback logic for other duration formats if needed
    return None
