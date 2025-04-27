import datetime
import json  # Import json for category handling
import uuid
from typing import Any, Dict, Optional, Tuple, cast
from urllib.parse import urlparse  # Import urlparse

import feedgen.feed  # type: ignore[import-untyped]
import feedparser  # type: ignore[import-untyped]
import validators  # Import validators
from feedparser.util import FeedParserDict  # type: ignore[import-untyped]
from flask import url_for
from sqlalchemy.exc import IntegrityError

from app import config, db, logger
from app.models import Feed, Post
from app.utils import parse_datetime  # type: ignore[import-not-found]
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
    explicit_val = data.get("itunes_explicit")  # Get raw value
    if explicit_val is None:
        return None  # Tag not present

    explicit_str = str(explicit_val).lower().strip()
    if explicit_str in ["yes", "true"]:
        return True
    # Treat 'no', 'false', 'clean', or any other non-empty value that isn't 'yes'/'true' as False
    if explicit_str:
        return False
    # Return None only if the tag was present but completely empty? Or default to false?
    # Let's default to False if present but empty/unrecognized for safety.
    return False


def _update_feed_attributes(feed: Feed, feed_data: FeedParserDict) -> bool:
    """Update feed attributes from fetched data. Returns True if changed."""
    updated = False

    # Define a helper within the function scope
    def _update_attr(attr_name: str, new_value: Any) -> None:
        nonlocal updated
        if new_value is not None and getattr(feed, attr_name) != new_value:
            setattr(feed, attr_name, new_value)
            updated = True  # Set outer scope variable

    # Update basic feed info
    image_info = _get_feed_attr(feed_data, "image", {})
    _update_attr("image_url", image_info.get("href"))
    _update_attr("title", _get_feed_attr(feed_data, "title"))
    _update_attr("description", _get_feed_attr(feed_data, "description"))
    _update_attr("author", _get_feed_attr(feed_data, "author"))

    # Update language and standard iTunes info
    _update_attr("language", _get_feed_attr(feed_data, "language"))
    _update_attr("itunes_author", _get_feed_attr(feed_data, "itunes_author"))
    _update_attr("itunes_subtitle", _get_feed_attr(feed_data, "itunes_subtitle"))
    _update_attr("itunes_summary", _get_feed_attr(feed_data, "itunes_summary"))
    _update_attr("itunes_type", _get_feed_attr(feed_data, "itunes_type"))

    # Update iTunes explicit (uses helper)
    new_itunes_explicit = _get_itunes_explicit(feed_data.feed)
    # Only update if the new value is not None (i.e., tag was present)
    if new_itunes_explicit is not None and feed.itunes_explicit != new_itunes_explicit:
        feed.itunes_explicit = new_itunes_explicit
        updated = True

    # Update iTunes owner
    owner = _get_feed_attr(feed_data, "itunes_owner", {})
    _update_attr("itunes_owner_name", owner.get("name"))
    _update_attr("itunes_owner_email", owner.get("email"))

    # Update keywords
    _update_attr("itunes_keywords", _get_feed_attr(feed_data, "itunes_keywords"))

    # Update iTunes categories (JSON comparison)
    new_categories = _get_feed_attr(feed_data, "tags")
    if new_categories:
        # Sort categories before dumping for consistent JSON string
        new_categories_json = json.dumps(
            sorted(new_categories, key=lambda x: x.get("term", "")), sort_keys=True
        )
        sorted_existing_json = None
        if feed.itunes_categories_json:
            try:
                existing_categories = json.loads(feed.itunes_categories_json)
                sorted_existing_json = json.dumps(
                    sorted(existing_categories, key=lambda x: x.get("term", "")),
                    sort_keys=True,
                )
            except json.JSONDecodeError:
                logger.warning(
                    f"Could not decode existing categories JSON for feed {feed.id}",
                    exc_info=True,
                )
                # Treat invalid JSON as different, sorted_existing_json remains None

        if sorted_existing_json != new_categories_json:
            feed.itunes_categories_json = new_categories_json
            updated = True

    return updated


def _process_feed_entry(
    feed: Feed, entry_data: feedparser.FeedParserDict
) -> Tuple[Optional[Post], bool]:
    """Process a single feed entry, creating or updating a Post object.

    Returns:
        Tuple[Optional[Post], bool]: The Post object (or None if skipped)
                                     and a boolean indicating if it was changed/added.
    """
    if not entry_data.id or not entry_data.published_parsed:
        logger.warning(
            f"Skipping entry due to missing ID or published date: {entry_data.get('title', 'N/A')}"
        )
        return None, False

    guid = entry_data.id
    published_date = parse_datetime(entry_data.published_parsed)

    post = db.session.get(Post, (feed.id, guid))
    changed = False

    # Extract common attributes
    title = entry_data.get("title")
    link = entry_data.get("link")
    description = entry_data.get("description") or entry_data.get("summary")
    duration_str = entry_data.get("itunes_duration")
    duration = get_duration(duration_str) if duration_str else None
    explicit = _get_itunes_explicit(entry_data)
    enclosure = next(
        (
            link
            for link in entry_data.get("links", [])
            if link.get("rel") == "enclosure"
        ),
        None,
    )
    enclosure_url = enclosure.get("href") if enclosure else None
    enclosure_length = int(enclosure.get("length", 0)) if enclosure else 0
    enclosure_type = enclosure.get("type") if enclosure else None

    if post:
        # Post exists, check for updates
        if post.published_date != published_date:
            post.published_date = published_date
            changed = True
        if post.title != title:
            post.title = title
            changed = True
        if post.link != link:
            post.link = link
            changed = True
        if post.description != description:
            post.description = description
            changed = True
        if post.duration != duration:
            post.duration = duration
            changed = True
        if post.explicit != explicit:
            post.explicit = explicit
            changed = True
        if post.enclosure_url != enclosure_url:
            post.enclosure_url = enclosure_url
            changed = True
        if post.enclosure_length != enclosure_length:
            post.enclosure_length = enclosure_length
            changed = True
        if post.enclosure_type != enclosure_type:
            post.enclosure_type = enclosure_type
            changed = True
    else:
        # Post does not exist, create it
        post = Post(
            feed_id=feed.id,
            guid=guid,
            published_date=published_date,
            title=title,
            link=link,
            description=description,
            duration=duration,
            explicit=explicit,
            enclosure_url=enclosure_url,
            enclosure_length=enclosure_length,
            enclosure_type=enclosure_type,
        )
        changed = True
        logger.info(f"Adding new post: {guid} - {title}")

    return post, changed


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    logger.info(f"Fetching feed from URL: {url}")
    feed_data = feedparser.parse(url)
    for entry in feed_data.entries:
        entry.id = get_guid(entry)
    return feed_data


def refresh_feed(feed: Feed) -> None:
    logger.info(f"Refreshing feed with ID: {feed.id}")
    feed_data = fetch_feed(feed.rss_url)

    # Update feed attributes using the helper function
    updated = _update_feed_attributes(feed, feed_data)

    if updated:
        db.session.add(feed)
        # Commit potentially later if also adding posts

    # Add new posts
    posts_changed = False
    for entry in feed_data.entries:
        post, entry_changed = _process_feed_entry(feed, entry)
        if post and entry.id:
            if entry_changed:
                db.session.add(post)  # Add to session only if new or changed
                posts_changed = True  # Track if any post changed

    # Remove posts no longer in the feed
    # Query existing posts directly from the DB for this feed
    # This avoids issues if feed.posts is not up-to-date before commit
    existing_posts_guids = {
        p.guid for p in Post.query.filter_by(feed_id=feed.id).with_entities(Post.guid)
    }
    guids_in_feed = {entry.id for entry in feed_data.entries if entry.id}
    guids_to_remove = existing_posts_guids - guids_in_feed
    if guids_to_remove:
        logger.info(
            f"Removing {len(guids_to_remove)} posts no longer in feed: {guids_to_remove}"
        )
        Post.query.filter(
            Post.feed_id == feed.id, Post.guid.in_(guids_to_remove)
        ).delete(synchronize_session=False)
        posts_changed = True  # Mark as changed if posts were deleted

    if updated or posts_changed:
        try:
            db.session.commit()
            logger.info(f"Committed feed/post changes for feed ID: {feed.id}")
        except IntegrityError as e:
            logger.error(
                f"Database integrity error committing changes for feed {feed.url}: {e}"
            )
            db.session.rollback()
        except (
            Exception  # pylint: disable=broad-exception-caught
        ) as e:  # Catch other potential commit errors
            logger.error(
                f"Error committing changes for feed {feed.url}: {e}", exc_info=True
            )
            db.session.rollback()
    else:
        logger.info(f"No feed/post changes detected for feed: {feed.url}")


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
    return feed  # type: ignore[no-any-return]


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
        std_summary = _get_feed_attr(
            feed_data, "summary", _get_feed_attr(feed_data, "description", "")
        )

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
            itunes_type=None,  # Not available via feedparser
            itunes_owner_name=None,  # Not available via feedparser
            itunes_owner_email=None,  # Not available via feedparser
            itunes_keywords=None,  # Not available via feedparser
            itunes_categories_json=categories_json,
        )
        db.session.add(feed)
        db.session.commit()  # Commit feed first to get feed.id

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
        db.session.commit()  # Commit posts
        logger.info(f"Feed stored with ID: {feed.id}")
        return feed
    except IntegrityError as e:
        logger.error(
            f"Database integrity error storing feed {feed_data.href}: {e}",
            exc_info=True,
        )
        db.session.rollback()
        raise e  # Re-raise after rollback
    except Exception as e:  # Catch other potential errors
        logger.error(f"Failed to store feed: {e}", exc_info=True)  # Log traceback
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
    fg.load_extension("podcast")  # Load the iTunes/Podcast extension

    # --- Feed Metadata (Moved Setup Here) ---
    feed_link = url_for("main.get_feed", f_id=feed.id, _external=True)
    fg.title("[podly] " + feed.title)
    fg.link(href=feed_link, rel="alternate")
    fg.description(feed.description or feed.itunes_summary or "")
    fg.language(feed.language or "en")
    if feed.image_url:
        fg.image(url=feed.image_url, title=feed.title, link=feed_link)
    fg.lastBuildDate(datetime.datetime.now(datetime.timezone.utc))
    fg.author({"name": feed.author or feed.itunes_author or "Podly"})

    # --- iTunes Feed Metadata ---
    fg.podcast.itunes_author(  # pylint: disable=no-member
        getattr(feed, "itunes_author", feed.author)
    )
    fg.podcast.itunes_subtitle(  # pylint: disable=no-member
        getattr(feed, "itunes_subtitle", "")
    )
    fg.podcast.itunes_summary(  # pylint: disable=no-member
        getattr(feed, "itunes_summary", feed.description)
    )  # noqa: E501

    # Apply image check/hack logic to channel image
    _set_channel_itunes_image(fg, feed)

    # Handle explicit tag generation (check if value is True/False)
    explicit_val = getattr(feed, "itunes_explicit", None)
    if explicit_val is not None:
        explicit_str = "yes" if explicit_val else "no"
        fg.podcast.itunes_explicit(explicit_str)  # pylint: disable=no-member
    fg.podcast.itunes_owner(  # pylint: disable=no-member
        name=getattr(feed, "itunes_owner_name", None),
        email=getattr(feed, "itunes_owner_email", None),
    )
    if getattr(feed, "itunes_type", None):
        fg.podcast.itunes_type(feed.itunes_type)  # pylint: disable=no-member

    # Add keywords as standard RSS categories
    keywords_str = getattr(feed, "itunes_keywords", None)
    if keywords_str:
        keywords_list = [k.strip() for k in keywords_str.split(",") if k.strip()]
        for keyword in keywords_list:
            fg.category(term=keyword)

    # Add iTunes categories from stored JSON
    _add_itunes_categories(fg, feed)

    # --- Feed Items (Posts) ---
    server_prefix = config.server or ""
    for post in sorted(
        feed.posts, key=lambda p: p.release_date or datetime.datetime.min, reverse=True
    ):
        _add_feed_entry_to_generator(fg, post, server_prefix)

    logger.info(f"Feedgen XML generated for feed with ID: {feed.id}")
    return cast(str, fg.rss_str(pretty=True))  # Generate the RSS XML string


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
            logger.warning(
                f"Could not parse release date for post: {entry.get('title', 'N/A')}"
            )

    # Extract episode-specific iTunes fields
    episode_type = _get_entry_attr(entry, "itunes_episodetype")
    image_url = _get_entry_attr(entry, "itunes_image")
    if not image_url:
        image_data = _get_entry_attr(entry, "image")
        if isinstance(image_data, dict):
            image_url = image_data.get("href")

    return Post(
        feed_id=feed.id,
        guid=entry.id,  # Use the pre-calculated GUID
        download_url=find_audio_link(entry),  # Assuming this finds the right URL
        title=_get_entry_attr(entry, "title", "Untitled Post"),
        description=description,
        release_date=release_date,
        duration=get_duration(entry),
        itunes_subtitle=itunes_subtitle,  # Will be None
        itunes_summary=itunes_summary,
        # Pass entry directly to helper
        itunes_explicit=_get_itunes_explicit(entry),
        itunes_episode_type=episode_type,
        itunes_image_url=image_url,
    )


# sometimes feed entry ids are the post url or something else
def get_guid(entry: feedparser.FeedParserDict) -> str:
    # Check for guid field first, as it's preferred
    guid = _get_entry_attr(entry, "guid")
    # ONLY use it if it's present AND explicitly marked as NOT a permalink
    if guid and _get_entry_attr(entry, "guid_ispermalink") is False:
        # Still double-check it doesn't look like a URL, just in case
        if not validators.url(str(guid)):
            return str(guid)

    # Check for atom id
    atom_id = _get_entry_attr(entry, "id")
    if atom_id:
        # Check if atom_id is a URL. If so, ignore it.
        if not validators.url(str(atom_id)):
            return str(atom_id)

    # Fallback to feedparser's default calculated 'id' field
    # Check if feedparser_id is a URL. If so, ignore it.
    try:
        feedparser_id = entry.id  # Access directly as it's calculated in fetch_feed
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
                pass  # Fall through to hashing

    except (AttributeError, KeyError):
        pass  # entry.id not present

    # --- Fallbacks ---
    # Hashing the download URL is usually the most reliable fallback
    dlurl = find_audio_link(entry)
    if dlurl:
        # Ensure we use a consistent hash
        return str(uuid.uuid5(uuid.NAMESPACE_URL, dlurl))

    # Absolute fallback: hash the title + date? Less ideal due to potential changes
    logger.warning(
        f"Could not find stable GUID (guid/id not URL, no dl_url) "
        f"for entry: {entry.get('title', 'N/A')}. Hashing title+date."
    )
    title_str = entry.get("title", "")
    date_str = str(entry.get("published_parsed", ""))
    # Use a different namespace maybe? Or just URL namespace is fine.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, title_str + date_str))


def get_duration(entry: feedparser.FeedParserDict) -> Optional[int]:
    """Attempts to parse duration (iTunes preferred) into seconds."""
    duration_str = _get_entry_attr(entry, "itunes_duration")
    if duration_str:
        try:
            parts = list(map(int, duration_str.split(":")))
            if len(parts) == 3:  # HH:MM:SS
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:  # MM:SS
                return parts[0] * 60 + parts[1]
            if len(parts) == 1:  # Seconds
                return parts[0]
        except (ValueError, TypeError):
            logger.warning(
                f"Could not parse duration '{duration_str}' for: {entry.get('title', 'N/A')}"
            )

    # Maybe add fallback logic for other duration formats if needed
    return None


# Helper function to process feedgen items
def _add_feed_entry_to_generator(
    fg: feedgen.feed.FeedGenerator, post: Post, server_prefix: str
) -> None:
    """Adds a single Post as an entry to the FeedGenerator."""
    fe = fg.add_entry()  # Create a feed entry

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

    fe.guid(post.guid, permalink=False)  # GUID is crucial
    fe.title(post.title or "Untitled Post")
    fe.link(href=post_details_url)  # Link to the Podly post page

    # Description: Use post.description, append Podly link
    desc_content = post.description or post.itunes_summary or ""
    podly_link_html = f'<p><a href="{post_details_url}">Podly Post Page</a></p>'
    description_html = f"{desc_content}\n{podly_link_html}"
    fe.description(description_html, isSummary=False)

    if post.release_date:
        # Ensure datetime is timezone-aware (assume UTC if not specified)
        release_date_aware = post.release_date
        if (
            isinstance(release_date_aware, datetime.datetime)
            and release_date_aware.tzinfo is None
        ):
            release_date_aware = release_date_aware.replace(
                tzinfo=datetime.timezone.utc
            )
        fe.pubDate(release_date_aware)

    # Enclosure (Audio File)
    fe.enclosure(
        url=podly_audio_url,
        length=str(post.audio_len_bytes()),  # Length must be string
        type="audio/mpeg",  # Assuming mp3, adjust if needed
    )

    # --- iTunes Item Metadata ---
    # Format duration manually
    duration_str = format_duration(getattr(post, "duration", None))
    if duration_str:
        try:
            fe.podcast.itunes_duration(duration_str)
        except (
            Exception  # pylint: disable=broad-exception-caught
        ) as e_dur:  # Use specific name
            logger.warning(
                f"Could not set duration '{duration_str}' for post {post.guid}. Error: {e_dur}",
                exc_info=True,
            )

    fe.podcast.itunes_subtitle(getattr(post, "itunes_subtitle", ""))
    fe.podcast.itunes_summary(
        getattr(post, "itunes_summary", post.description)
    )  # noqa: E501
    # Handle explicit tag generation
    item_explicit_val = getattr(post, "itunes_explicit", None)
    if item_explicit_val is not None:
        item_explicit_str = "yes" if item_explicit_val else "no"
        fe.podcast.itunes_explicit(item_explicit_str)
    fe.podcast.itunes_episode_type(
        getattr(post, "itunes_episode_type", None)
    )  # noqa: E501

    # Apply image check/hack logic to item image
    episode_image_url = getattr(post, "itunes_image_url", None)
    if episode_image_url:
        try:
            if episode_image_url.lower().endswith((".png", ".jpg")):
                fe.podcast.itunes_image(episode_image_url)
            else:  # noqa: E501
                hacked_url = _apply_image_hack(episode_image_url)
                if hacked_url:
                    try:
                        fe.podcast.itunes_image(hacked_url)
                    except (
                        ValueError
                    ) as ve:  # Use specific error for image hack failure # pylint: disable=broad-exception-caught
                        logger.warning(
                            f"Value error processing episode image {episode_image_url}: {ve}",
                            exc_info=True,
                        )
        except (
            ValueError
        ) as ve:  # Use specific error for image hack failure # pylint: disable=broad-exception-caught
            logger.warning(
                f"Value error processing episode image {episode_image_url}: {ve}",
                exc_info=True,
            )
        except (
            Exception  # pylint: disable=broad-exception-caught
        ) as img_ex:  # Catch other unexpected errors
            logger.warning(
                f"Error processing episode image {episode_image_url}: {img_ex}",
                exc_info=True,
            )


# --- Feed Generation Helpers ---


def _apply_image_hack(image_url: str) -> Optional[str]:
    """Applies the '.jpg' hack to an image URL if needed and logs a warning."""
    parsed_url = urlparse(image_url)
    separator = "&" if parsed_url.query else "?"
    hacked_url = image_url + separator + "feedgen=.jpg"
    logger.warning(
        f"Image URL '{image_url}' lacks extension. " f"Attempting hack: '{hacked_url}'"
    )
    return hacked_url


def _set_channel_itunes_image(fg: feedgen.feed.FeedGenerator, feed: Feed) -> None:
    """Sets the iTunes channel image, applying URL validation and hack if needed."""
    channel_image_url = getattr(feed, "image_url", None)
    if not channel_image_url:
        return

    try:
        # Check if the ORIGINAL url already satisfies feedgen
        if channel_image_url.lower().endswith((".png", ".jpg")):
            fg.podcast.itunes_image(channel_image_url)
        else:  # noqa: E501
            # Original URL failed validation, try the hack
            hacked_url = _apply_image_hack(channel_image_url)
            if hacked_url:
                try:
                    fg.podcast.itunes_image(hacked_url)
                except ValueError:
                    logger.error(
                        f"Hack failed for channel image URL '{channel_image_url}'. "
                        f"Skipping itunes:image for feed {feed.id}."
                        # noqa: E501
                    )
    except (
        Exception  # pylint: disable=broad-exception-caught
    ) as img_ex:  # Catch other unexpected errors
        logger.warning(
            f"Error processing channel image {channel_image_url}: {img_ex}",
            exc_info=True,  # Include traceback
        )


def _add_itunes_categories(fg: feedgen.feed.FeedGenerator, feed: Feed) -> None:
    """Adds iTunes categories from the feed's JSON data to the FeedGenerator."""
    categories_json = getattr(feed, "itunes_categories_json", None)
    if not categories_json:
        return

    try:
        categories = json.loads(categories_json)
        if not isinstance(categories, list):
            logger.warning(f"Categories JSON is not a list for feed {feed.id}")
            return

        processed_cats = set()
        for cat_data in categories:
            if not isinstance(cat_data, dict):
                continue  # Skip invalid category entries
            term = cat_data.get("term")
            if not term or term in processed_cats:
                continue  # Skip empty terms or duplicates

            # Add recognized/valid iTunes categories (expand list as needed)
            # This check could be made more robust (e.g., comparing against a predefined set)
            if term in ["News", "Daily News"]:
                fg.podcast.itunes_category(term)
                processed_cats.add(term)
            # else: # Optionally log skipped non-standard terms
            # logger.debug(f"Skipping non-standard term: {term}")

    except (
        json.JSONDecodeError
    ):  # Specific error first # pylint: disable=broad-exception-caught
        logger.warning(
            f"Could not decode categories JSON for feed {feed.id}", exc_info=True
        )
    except (
        Exception  # pylint: disable=broad-exception-caught
    ) as cat_ex:  # Catch other potential errors during category processing
        logger.warning(
            f"Error processing categories for feed {feed.id}: {cat_ex}", exc_info=True
        )
