from odoo import api, fields, models


class DevopsMeeting(models.Model):
    _name = 'devops.meeting'
    _description = 'DevOps Meeting'
    _order = 'date desc'

    name = fields.Char(string='Titulo', required=True)
    project_id = fields.Many2one('devops.project', string='Proyecto', required=True, ondelete='cascade')
    instance_id = fields.Many2one('devops.instance', string='Instancia')
    user_id = fields.Many2one('res.users', string='Creado por', default=lambda self: self.env.uid)
    date = fields.Datetime(string='Fecha', default=fields.Datetime.now)
    meet_url = fields.Char(string='URL de Meet')
    notes = fields.Text(string='Notas')
    transcription = fields.Text(string='Transcripcion')
    audio_filename = fields.Char(string='Audio filename')
    audio_file = fields.Binary(string='Audio', attachment=True)
    state = fields.Selection([
        ('scheduled', 'Programada'),
        ('in_progress', 'En curso'),
        ('done', 'Finalizada'),
        ('transcribed', 'Transcrita'),
    ], default='scheduled', string='Estado')
    duration_minutes = fields.Integer(string='Duracion (min)')
