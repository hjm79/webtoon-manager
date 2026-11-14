from app import auto_subscribe, subscription_scope

async def automatic_scope(platform, title_id, settings):
    policy = auto_subscribe.scope_config()
    episodes = [{'episode_no': n, 'available': True, 'owned': False} for n in range(1, 4)]
    return policy, subscription_scope.make_choice(platform, title_id, policy['mode'], episodes, latest_count=policy['latest_count'])
