import argparse
from collections import Counter

from utils import safe_post_request, depaginated_request

STAFF_COL_WIDTH = 20
SHOW_COL_WIDTH = 40
COL_SEP = 3


# Ideally we could sort on [SEARCH_MATCH, POPULARITY_DESC], but this doesn't seem to work as expected in the case of
# shows with the exact same title (e.g. Golden Time); the less popular one is still returned.
def get_show(search, sort_by="SEARCH_MATCH"):
    """Given an approximate show name, return the closest-matching show with ID and title.
    Default sorts by closeness of the string match. Use e.g. POPULARITY_DESC for cases where shows share a name (e.g.
    "Golden Time" will by default return the one no one cares about).
    """
    query = '''
query ($search: String, $sort: MediaSort) {
    Media(search: $search, type: ANIME, sort: [$sort]) {
        id
        title {
            english
            romaji
        }
    }
}'''
    result = safe_post_request({'query': query, 'variables': {'search': search, 'sort': sort_by}})
    if result is not None:
        result = result['Media']

        # In case a show has no english title, fall back to romaji
        title = result['title']['english'] if result['title']['english'] is not None else result['title']['romaji']
        assert title is not None, f"API returned an untitled show for \"{search}\" (show ID: {result['id']})"

        result = {'id': result['id'], 'title': title}

    return result


def get_show_studios(show_id):
    """Given a show ID, return a dict of its studios, formatted as id: {"name": "...", "roles": ["..."]}."""
    query = '''
query ($mediaId: Int) {
    Media(id: $mediaId) {
        studios {
            edges {
                node {
                    id
                    name
                }
                isMain
            }
        }
    }
}'''
    # Since the API doesn't sort by isMain, handle main vs supporting studios separately, so we can return main
    # studio(s) at the front of the results
    main_studios_dict = {}
    supporting_studios_dict = {}

    # the Media.studios API also does not seem to be paginated even though StudioConnection has pageInfo
    for edge in safe_post_request({'query': query, 'variables': {'mediaId': show_id}})['Media']['studios']['edges']:
        if edge['isMain']:
            main_studios_dict[edge['node']['id']] = {'name': edge['node']['name'], 'roles': ["Main"]}
        else:
            supporting_studios_dict[edge['node']['id']] = {'name': edge['node']['name'], 'roles': ["Supporting"]}

    return main_studios_dict | supporting_studios_dict


def get_show_production_staff(show_id):
    """Given a show ID, return a dict of its production staff, formatted as id: {"name": "...", "roles": ["..."]}."""
    query = '''
query ($mediaId: Int, $page: Int, $perPage: Int) {
    Media(id: $mediaId) {
        staff(sort: RELEVANCE, page: $page, perPage: $perPage) {
            pageInfo {
                hasNextPage
            }
            # Direct `nodes` field is also available, but it includes duplicates per edge (e.g. one staff with two roles
            # shows up twice), so avoiding it to keep things intuitive.
            edges {
                node {
                    id
                    name {
                        full
                    }
                }
                role
            }
        }
    }
}'''
    staff_dict = {}

    for edge in depaginated_request(query=query, variables={'mediaId': show_id}):
        # Account for staff potentially having multiple roles
        if edge['node']['id'] not in staff_dict:
            staff_dict[edge['node']['id']] = {'name': edge['node']['name']['full'],
                                              'roles': []}

        staff_dict[edge['node']['id']]['roles'].append(edge['role'])

    return staff_dict


def get_show_voice_actors(show_id, language="JAPANESE"):
    """Given a show ID, return a dict of its voice actors for the given language (default: "JAPANESE"), formatted as:
    id: {"name": "...", "roles": ["MAIN: Edward Elric", "SUPPORTING: Edward Elric (child)"]}.
    """
    query = '''
query ($mediaId: Int, $language: StaffLanguage, $page: Int, $perPage: Int) {
    Media(id: $mediaId) {
        characters(sort: [ROLE, RELEVANCE], page: $page, perPage: $perPage) {
            pageInfo {
                hasNextPage
            }
            edges {
                node {  # Character
                    name {
                        full
                    }
                }
                role  # MAIN, SUPPORTING, or BACKGROUND
                voiceActorRoles(language: $language) {  # This is a list, but the API doesn't make us paginate it
                    voiceActor {
                        id
                        name {
                            full
                        }
                    }
                    roleNotes
                }
            }
        }
    }
}'''
    vas_dict = {}

    for edge in depaginated_request(query=query, variables={'mediaId': show_id, 'language': language}):
        for va_role in edge['voiceActorRoles']:
            # Account for VAs potentially having multiple roles
            if va_role['voiceActor']['id'] not in vas_dict:
                vas_dict[va_role['voiceActor']['id']] = {'name': va_role['voiceActor']['name']['full'],
                                                         'roles': []}

            role_descr = edge['role'] + " " + edge['node']['name']['full']
            if va_role['roleNotes'] is not None:
                role_descr += " " + va_role['roleNotes']

            vas_dict[va_role['voiceActor']['id']]['roles'].append(role_descr)

    return vas_dict


def get_production_staff_shows(staff_id):
    """Given a staff id, return a set of shows they've been a production staff member for, as (show_id, show_title)."""
    query = '''
query ($staffId: Int, $page: Int, $perPage: Int) {
    Staff(id: $staffId) {
        staffMedia(type: ANIME, sort: POPULARITY_DESC, page: $page, perPage: $perPage) {
            pageInfo {
                hasNextPage
            }
            nodes {
                id
                title {
                    english
                    romaji
                }
            }
        }
    }
}'''
    # Return both ID and title to save a query
    return {(show['id'], show['title']['english'] if show['title']['english'] is not None else show['title']['romaji'])
            for show in depaginated_request(query=query, variables={'staffId': staff_id})}


def dict_intersection(dicts):
    """Given an iterable of dicts, return a list of the intersection of their keys, while preserving the order of the
    keys from the first given dict."""

    dicts = list(dicts)  # Avoid gotchas if we were given an iterator
    if not dicts:
        return []

    return [k for k in dicts[0] if all(k in d for d in dicts[1:])]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Find all studios/staff/VAs common to all of the given shows.\n"
                    "If given only one show, list shows with highest numbers of shared staff and compare to the top"
                    " match.",
        formatter_class=argparse.RawTextHelpFormatter)  # Preserves newlines in help text
    parser.add_argument('shows', nargs='+', help="Show(s) to compare.")
    parser.add_argument('-t', '--top', type=int, default=5,
                        help="How many top matching shows to list when given only one show. Default 5.")
    parser.add_argument('-p', '--popularity', action='store_true',
                        help="Match more popular shows instead of the closest string matches to the given show names.\n"
                             "Helpful in cases like e.g. Golden Time where another show of the same name exists.")
    args = parser.parse_args()

    show_ids = []
    show_titles = []
    show_studios_dicts = []
    show_production_staff_dicts = []
    show_voice_actors_dicts = []

    # Lookup each show by name and collect data on their staff
    for show in args.shows:
        show_data = get_show(show, sort_by='POPULARITY_DESC' if args.popularity else 'SEARCH_MATCH')
        if show_data is None:
            raise ValueError(f"Could not find show matching {show}")

        show_ids.append(show_data['id'])
        show_titles.append(show_data['title'])
        show_studios_dicts.append(get_show_studios(show_data['id']))
        show_production_staff_dicts.append(get_show_production_staff(show_data['id']))
        show_voice_actors_dicts.append(get_show_voice_actors(show_data['id'], language="JAPANESE"))

    # If given only one show, find the show with the most shared production staff and compare it
    # TODO: Also find anime by similarity of animation staff vs script/directors vs music vs VAs
    if len(args.shows) == 1:
        if len(show_production_staff_dicts[0]) > 70:
            print(f"Searching for other shows worked on by staff of `{show_titles[0]}`, this may take a couple minutes...")

        # Query each staff member for the IDs of all anime they've had production roles in and keep a tally
        # TODO: This takes prohibitively many queries. We can be more clever and exit slightly early once a show is in
        #       the lead by >= num_remaining_staff (or if we list top N, when the Nth is that far ahead of (N + 1)th).
        #       After exiting early if we want the exact staff counts we can query the top shows directly for their
        #       staff, which takes far fewer queries.
        show_counts = Counter()
        for staff_id in show_production_staff_dicts[0]:
            show_counts.update(get_production_staff_shows(staff_id))  # Returns (ID, title) tuples

        if len(show_counts) <= 1:  # 1 since the results will include itself
            print(f"Staff for {show_titles[0]} have not done any other shows.")
            exit()

        # Report the top 5 matching shows and add the top one for comparison.
        # Make sure to ignore the given show as it will always have the most matches. However check for its ID instead
        # of blindly skipping the top match, just in case of ties (e.g. an unreleased show with very few staff listed
        # might be completely supersetted).
        top_shows = [item for item in show_counts.most_common(args.top + 1) if item[0][0] != show_ids[0]]
        print(f"Shows with most production staff in common with {show_titles[0]}:")
        for (other_show_id, other_show_title), shared_staff_count in top_shows:
            print(f"    {shared_staff_count:2} | {other_show_title[:SHOW_COL_WIDTH]}")
        print("\n")

        (other_show_id, other_show_title), shared_staff_count = top_shows[0]
        show_ids.append(other_show_id)  # Unused, but for consistency
        show_titles.append(other_show_title)
        show_studios_dicts.append(get_show_studios(other_show_id))
        show_production_staff_dicts.append(get_show_production_staff(other_show_id))
        show_voice_actors_dicts.append(get_show_voice_actors(other_show_id, language="JAPANESE"))

    col_widths = [STAFF_COL_WIDTH] + [SHOW_COL_WIDTH] * len(show_titles)
    total_width = sum(col_widths) + COL_SEP * (len(col_widths) - 1)  # Adjust for separator

    def col_print(items):
        """Print the given strings left-justified in the appropriate width columns, truncating them if too long."""
        print((COL_SEP * ' ').join(item[:col_width].ljust(col_width) for item, col_width in zip(items, col_widths)))

    col_print([""] + show_titles)

    # List common studios/staff, sectioned separately by studios vs production staff vs voice actors
    common_found = False
    for staff_type, show_staff_dicts in [["Studios", show_studios_dicts],
                                         ["Production Staff", show_production_staff_dicts],
                                         ["Voice Actors (JP)", show_voice_actors_dicts]]:
        # Find the common staff between the shows. Use a helper to avoid sets so that dict ordering is maintained
        common_staff_ids = dict_intersection(show_staff_dicts)

        if common_staff_ids:
            if common_found:  # Quick hack to avoid leading newlines
                print("\n")
            common_found = True

            print(staff_type)
            print("═" * total_width)

            for staff_id in common_staff_ids:
                # Print a row(s) with the staff name followed by their role(s) in each show
                max_roles = max(len(show_staff[staff_id]['roles']) for show_staff in show_staff_dicts)
                for i in range(max_roles):
                    cols = [show_staff_dicts[0][staff_id]['name'] if i == 0 else ""]
                    cols.extend((show_staff[staff_id]['roles'][i] if i < len(show_staff[staff_id]['roles']) else "")
                                for show_staff in show_staff_dicts)
                    col_print(cols)

    if not common_found:
        print("")
        print("No common studios/staff/VAs found!".center(total_width))
