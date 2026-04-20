import os
import json

def parse_env_file(env_file_path=".env"):
    """Parse .env file and return list of dicts for Azure format."""
    # Keys to exclude from Azure conversion (Azure manages these dynamically)
    EXCLUDED_KEYS = {'PORT', 'WEBSITES_PORT'}
    
    env_map = {}
    excluded_vars = []
    with open(env_file_path, 'r') as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Split on first '=' sign
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes (double or single) from values
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                # Unescape any escaped quotes that were inside the original quotes
                value = value.replace('\\"', '"').replace("\\'", "'")
                if key and value:
                    if key in EXCLUDED_KEYS:
                        excluded_vars.append(key)
                        continue
                    env_map[key] = value
    
    # Report excluded variables
    if excluded_vars:
        print(f"⚠️  Excluded from Azure conversion (Azure-managed): {', '.join(excluded_vars)}")

    env_vars = [
        {
            "name": k,
            "value": v,
            "slotSetting": False
        }
        for k, v in env_map.items()
    ]
    return env_vars

def save_azure_env_settings(env_vars, folder=".azure-temp-env-files", filename="azure_env_settings.json"):
    """Save env vars to JSON file in specified folder."""
    # Create folder if it doesn't exist
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)
    with open(filepath, 'w') as f:
        json.dump(env_vars, f, indent=2)
    print(f"Saved to {filepath}")

def update_gitignore(folder=".azure-temp-env-files"):
    """Add folder to .gitignore if not already present."""
    gitignore_path = ".gitignore"
    ignore_entry = f"{folder}/\n"
    if os.path.exists(gitignore_path):
        with open(gitignore_path, 'r') as f:
            content = f.read()
        if ignore_entry.strip() not in content:
            with open(gitignore_path, 'a') as f:
                f.write(ignore_entry)
    else:
        # Create .gitignore if it doesn't exist
        with open(gitignore_path, 'w') as f:
            f.write(ignore_entry)
    print(".gitignore updated")

if __name__ == "__main__":
    env_vars = parse_env_file()
    save_azure_env_settings(env_vars)
    update_gitignore()
