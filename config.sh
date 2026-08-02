## ComfyUI-Nvidia-Docker configuration
# loaded by init.bash as /comfyui-nvidia_config.sh
# ... after setting the variables from the command line: will override with the values set here
#
# To use your custom version, duplicate the file and mount it in the container: -v /path/to/your/config.sh:/comfyui-nvidia_config.sh
#
# Can be used to set the other command line variables
# Set using: export VARIABLE=value

## Environment variables loaded when passing environment variables from user to user
# Ignore list: variables to ignore when loading environment variables from user to user
export ENV_IGNORELIST="HOME PWD USER SHLVL TERM OLDPWD SHELL _ SUDO_COMMAND HOSTNAME LOGNAME MAIL SUDO_GID SUDO_UID SUDO_USER CHECK_NV_CUDNN_VERSION VIRTUAL_ENV VIRTUAL_ENV_PROMPT ENV_IGNORELIST ENV_OBFUSCATE_PART"
# Obfuscate part: part of the key to obfuscate when loading environment variables from user to user, ex: HF_TOKEN, ...
export ENV_OBFUSCATE_PART="TOKEN API KEY"

########## Command line variables
# Uncomment and set as preferred, see README.md for more details

# User and group id
#export WANTED_UID=1000
#export WANTED_GID=1000
# DO NOT use `id -u` or `id -g` to set the values, use the actual values -- the script is started by comfytoo with 1025/1025

# Use socat to listen on port 8188 and forward to 127.0.0.1:8181 (ie use an alternate port for Comfy to run)
# default is false, value must set to "true" to enable
#export USE_SOCAT="true"

# Use pip upgrade: new default is to use pip install --upgrade, set to "false" to use pip install
# default is true, value must set to "false" to disable
#export USE_PIPUPGRADE="false"

# Disable upgrades: set to "true" to disable upgrades (also disables USE_PIPUPGRADE)
#export DISABLE_UPGRADES="true"

# Base directory
#export BASE_DIRECTORY="/basedir"

# Security level
#export SECURITY_LEVEL="weak"

# ComfyUI command line extra
#export COMFY_CMDLINE_EXTRA="--fast --use-sage-attention"
# Force chown: force chown mode enabled, will force change directory ownership as comfy user during script rerun (might be slow)
#export FORCE_CHOWN="false"

# PREINSTALL_TORCH: preinstall torch: default is true, set to "false" to disable
#export PREINSTALL_TORCH="false"
# PREINSTALL_TORCH_CMD: when PREINSTALL_TORCH is set to true, will use the command specified in this variable to install torch
#export PREINSTALL_TORCH_CMD="pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126"

# uv is included in current images. Leave UPDATE_UV disabled for reproducible
# starts; opt in only when intentionally testing a newer uv release.
#export USE_UV="true"
#export UPDATE_UV="false"

## NVIDIA specific adds
#export NVIDIA_VISIBLE_DEVICES=all
#export NVIDIA_DRIVER_CAPABILITIES=all
#export NVCC_APPEND_FLAGS='-allow-unsupported-compiler'

## User settings
# If adding content to be obfuscated, add it to ENV_OBFUSCATE_PART
#export HF_TOKEN=""
#export OPENAI_API_KEY=""


# Do not use an exit code, this is loaded by source
