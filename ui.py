"""Shared application chrome for all three views."""
import html


def header(location, active):
    links = ''.join(
        f'<a href="{path}"' + (' class="active" aria-current="page"' if key == active else '') + f'>{label}</a>'
        for key, path, label in [('table', 'overview.html', 'Table'), ('map', 'map.html', 'Map'), ('activity', 'activity.html', 'Activity')]
    )
    return f'<div class="app-header"><h1>funda-search <span>· {html.escape(str(location))}</span></h1><nav class="views" aria-label="View">{links}</nav></div>'
