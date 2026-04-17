{
    'name': 'PatchMyByte DevOps',
    'version': '19.0.1.1.6',
    'category': 'Services/DevOps',
    'summary': 'Plataforma DevOps multi-proyecto estilo Odoo.sh',
    'description': """
PatchMyByte DevOps
==================
Administra múltiples instancias Odoo desde una plataforma central.

* Terminal web (Claude AI, Shell, Logs)
* Git branch management y Git Graph
* Builds & Deployments automatizados
* Backups con pg_dump y retención
* Deploy con IA (tests + backup + restart + rollback)
* Plugins de Claude Code
* Roles multi-usuario por proyecto
""",
    'depends': ['base', 'mail', 'project'],
    'data': [
        'security/devops_security.xml',
        'security/ir.model.access.csv',
        'data/devops_data.xml',
        'data/devops_cron.xml',
        'wizard/ai_assistant_wizard_views.xml',
        'wizard/deploy_wizard_views.xml',
        'wizard/claude_login_wizard_views.xml',
        'views/devops_project_views.xml',
        'views/devops_branch_views.xml',
        'views/devops_build_views.xml',
        'views/devops_log_views.xml',
        'views/devops_backup_views.xml',
        'views/devops_deploy_ai_views.xml',
        'views/devops_plugin_views.xml',
        'views/res_config_settings_views.xml',
        'views/devops_instance_views.xml',
        'views/devops_menus.xml',
    ],
    'application': True,
    'installable': True,
    'post_init_hook': '_post_init_hook',
    'assets': {
        'web.assets_backend': [
            'pmb_devops/static/src/pmb_app/pmb_app.scss',
            'pmb_devops/static/src/pmb_app/pmb_app.js',
            'pmb_devops/static/src/pmb_app/pmb_app.xml',
            'pmb_devops/static/src/terminal/devops_terminal.js',
            'pmb_devops/static/src/terminal/devops_terminal.xml',
            'pmb_devops/static/src/user_menu_items.js',
        ],
    },
    'author': 'PatchMyByte',
    'license': 'LGPL-3',
}
