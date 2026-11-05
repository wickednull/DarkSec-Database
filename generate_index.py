import os

def generate_subdirectory_index(dir_name, files):
    with open("index.template.html", "r") as f:
        template = f.read()

    grid_content = ""
    for file in files:
        if file != "index.html":
            grid_content += f'<a class="card" href="{file}"><b>{file}</b></a>\n'

    new_index_html = template.replace("{{GRID_CONTENT}}", grid_content)
    new_index_html = new_index_html.replace("{{DIR_NAME}}", dir_name.upper())
    new_index_html = new_index_html.replace('<p class="note">Drop PDFs into any category folder; run this script (or <code>./darksec-atlas_survival_database.sh reindex</code>) to refresh.</p>', '<p class="note"><a href="../index.html">..</a></p>')


    with open(os.path.join(dir_name, "index.html"), "w") as f:
        f.write(new_index_html)

def generate_index():
    # Define the directories to be indexed
    directories = {
        "chembio": "Chemical, biological, radiological, and nuclear survival",
        "comms": "ARES/WRN guides, antennas, propagation, signaling",
        "engineering": "Electrical, plumbing, welding, diesel, solar/wind",
        "manuals": "Survival, SERE, Ranger, British Fieldcraft",
        "medical": "First aid, hygiene, herbal medicine, and more",
        "prepper": "Security hardening, checklists, psychology",
        "water_food": "Preservation, canning, disinfection, hydroponics",
        "isos": "Rescue/OS image links",
        "maps": "Land nav, celestial, USGS symbols",
        "hacking": "Hacking tools and guides"
    }

    grid_content = ""
    files_to_cache = ["/", "index.html", "guide.png", "manifest.json"]

    for dir_name, description in directories.items():
        dir_path = os.path.join(os.path.dirname(__file__), dir_name)
        if os.path.isdir(dir_path):
            files = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]
            file_count = len(files)
            if file_count > 0:
                grid_content += f'''<a class="card" href="{dir_name}/index.html"><b>[{dir_name.upper()}]</b> {file_count} files available.<span class="desc">{description}</span></a>
'''
                files_to_cache.append(f"{dir_name}/index.html")
                generate_subdirectory_index(dir_name, files)
                for file in files:
                    files_to_cache.append(f"{dir_name}/{file}")


    # Update index.html
    with open("index.template.html", "r") as f:
        template = f.read()

    new_index_html = template.replace("{{GRID_CONTENT}}", grid_content)
    new_index_html = new_index_html.replace("{{DIR_NAME}}", "DarkSec Survival Database")


    with open("index.html", "w") as f:
        f.write(new_index_html)

    # Update sw.js
    with open("sw.js", "r") as f:
        sw_content = f.read()

    urls_to_cache_string = "const urlsToCache = [\n"
    for file_path in files_to_cache:
        urls_to_cache_string += f"  '{file_path}',\n"
    urls_to_cache_string += "];"

    # It's tricky to replace the array with a regex, so we'll just replace the whole file content
    # This is brittle, but for this specific case it's the simplest solution.
    start_marker = "const CACHE_NAME = 'darksec-survival-db-cache-v1';"
    end_marker = "self.addEventListener('install', function(event) {"
    
    new_sw_content = sw_content.split(start_marker)[0] + start_marker + "\n" + urls_to_cache_string + "\n\n" + end_marker + sw_content.split(end_marker)[1]


    with open("sw.js", "w") as f:
        f.write(new_sw_content)

if __name__ == "__main__":
    generate_index()
    print("Homepage and service worker updated successfully.")