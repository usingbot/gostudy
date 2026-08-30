from optional_features import feature_enabled


this_package = 'modules'

active = [
    '.sysadmin',
    '.config',
    '.user_config',
    '.skins',
    '.schedule',
    '.economy',
    '.ranks',
    '.reminders',
    '.shop',
    '.statistics',
    '.pomodoro',
    '.rooms',
    '.tasklist',
    '.rolemenus',
    '.member_admin',
    '.moderation',
    '.video_channels',
    '.gostudy_rewards',
    '.gostudy_chalk',
    '.gostudy_guild_registry',
    '.meta',
    '.sponsors',
]


def configured_extensions(config):
    extensions = list(active)
    if feature_enabled(config, 'TOPGG'):
        extensions.append('.topgg')
    if feature_enabled(config, 'PREMIUM'):
        extensions.append('.premium')
    return extensions


async def setup(bot):
    for ext in configured_extensions(bot.config.config):
        await bot.load_extension(ext, package=this_package)
