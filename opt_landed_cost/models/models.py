# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.addons import decimal_precision as dp


class LandedCost(models.Model):
    _inherit = 'stock.landed.cost'

    @api.model
    def _default_currency(self):
        return self.env.user.company_id.currency_id

    currency_id = fields.Many2one('res.currency', string='Currency',
                                  required=True, readonly=True, states={'draft': [('readonly', False)]},
                                  default=_default_currency, track_visibility='always')

    amount_total = fields.Monetary('Total', compute='_compute_total_amount', digits=0, store=True,
                                   track_visibility='always')

    # Tipo de Cambio
    other_currency = fields.Boolean(string="Other Corrency", compute="_compute_other_currency", store=True, copy=False)
    exchange_date = fields.Date(string="Fecha de Cambio")
    user_exchange_rate = fields.Boolean(string="Tipo de Cambio Usuario")
    exchange_rate = fields.Float(string="Tipo de Cambio", digits=(12, 3), store=True, readonly=False)

    @api.multi
    @api.depends('currency_id')
    def _compute_other_currency(self):
        for rec in self:
            company_id = rec.company_id or self.env.user.company_id
            if company_id and rec.currency_id and rec.currency_id != company_id.currency_id:
                rec.other_currency = True
            else:
                rec.other_currency = False

    @api.onchange('currency_id', 'exchange_date', 'user_exchange_rate')
    def _get_exchange_rate(self):
        if self.currency_id == self.company_id.currency_id:
            self.user_exchange_rate = False
        if self.exchange_date:
            if self.currency_id != self.company_id.currency_id and not self.user_exchange_rate:
                domain = [('currency_id.id', '=', self.currency_id.id),
                          ('name', '=', fields.Date.to_string(self.exchange_date)),
                          ('company_id.id', '=', self.company_id.id)]
                currency = self.env['res.currency.rate'].search(domain, limit=1)
                if currency:
                    self.exchange_rate = currency.rate_pe
                else:
                    if self.currency_id:
                        self.exchange_date = False
                        self.exchange_rate = 0
                        # raise ValidationError("No se encontro el tipo de cambio para la fecha seleccionada")
                        # message = _('No se encontro el tipo de cambio para la fecha seleccionada')
                        # warning_mess = {'title': _('Payment is Pending!'), 'message': message}
                        # return {'warning': warning_mess}
            else:
                self.exchange_rate = 0
                self.exchange_date = False
        else:
            self.exchange_rate = 0
            self.exchange_date = False

    def get_valuation_lines(self):
        lines = []
        for move in self.mapped('picking_ids').mapped('move_lines'):
            # it doesn't make sense to make a landed cost for a product that isn't set as being valuated in real time at real cost
            if move.product_id.valuation != 'real_time' or move.product_id.cost_method not in 'fifo,average':
                continue
            vals = {
                'product_id': move.product_id.id,
                'move_id': move.id,
                'quantity': move.product_qty,
                'former_cost': move.value,
                'weight': move.product_id.weight * move.product_qty,
                'volume': move.product_id.volume * move.product_qty
            }
            lines.append(vals)

        if not lines and self.mapped('picking_ids'):
            raise UserError(_(
                "You cannot apply landed costs on the chosen transfer(s). Landed costs can only be applied for products with automated inventory valuation and FIFO costing method."))
        return lines

    @api.multi
    def _calculated_cost(self):
        for rec in self:
            if rec.state == 'done':
                rec.valuation_adjustment_lines._calculated_cost()


class LandedCostLine(models.Model):
    _inherit = 'stock.landed.cost.lines'

    currency_id = fields.Many2one('res.currency', related='cost_id.currency_id', store=True, related_sudo=False,
                                  readonly=False)
    price_unit = fields.Monetary('Cost', digits=dp.get_precision('Product Price'), required=True)


class AdjustmentLines(models.Model):
    _inherit = 'stock.valuation.adjustment.lines'

    calculated_cost = fields.Boolean(string='Costo Calculado', default=False)

    historical_cost = fields.Float(string="Costo Historico")

    # move_line_id = fields.Many2one('stock.move.line', 'Stock Move Line', copy=False)

    currency_id = fields.Many2one('res.currency', related='cost_id.company_id.currency_id', readonly=True)
    original_currency_id = fields.Many2one('res.currency', related='cost_id.currency_id', readonly=True)

    additional_landed_cost = fields.Monetary('Additional Landed Cost',
                                             currency_field='original_currency_id',
                                             digits=dp.get_precision('Product Price'))

    additional_landed_cost_final = fields.Monetary('Additional Landed Cost Final',
                                                   compute="_compute_additional_landed_cost_final")

    def _create_account_move_line(self, move, credit_account_id, debit_account_id, qty_out, already_out_account_id):
        """
        Generate the account.move.line values to track the landed cost.
        Afterwards, for the goods that are already out of stock, we should create the out moves
        """
        AccountMoveLine = []

        base_line = {
            'name': self.name,
            'product_id': self.product_id.id,
            'quantity': 0,
        }
        debit_line = dict(base_line, account_id=debit_account_id)
        credit_line = dict(base_line, account_id=credit_account_id)
        diff = self.additional_landed_cost
        if diff > 0:
            debit_line['debit'] = diff
            credit_line['credit'] = diff
            if self.cost_id and self.cost_id.other_currency:
                debit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                debit_line['amount_currency'] = diff
                debit_line['debit'] = diff * self.cost_id.exchange_rate
                credit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                credit_line['amount_currency'] = diff * -1
                credit_line['credit'] = diff * self.cost_id.exchange_rate
        else:
            # negative cost, reverse the entry
            debit_line['credit'] = -diff
            credit_line['debit'] = -diff
            if self.cost_id and self.cost_id.other_currency:
                debit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                debit_line['amount_currency'] = -diff
                debit_line['debit'] = -diff * self.cost_id.exchange_rate
                credit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                credit_line['amount_currency'] = diff
                credit_line['credit'] = -diff * self.cost_id.exchange_rate
        AccountMoveLine.append([0, 0, debit_line])
        AccountMoveLine.append([0, 0, credit_line])

        # Create account move lines for quants already out of stock
        if qty_out > 0:
            debit_line = dict(base_line,
                              name=(self.name + ": " + str(qty_out) + _(' already out')),
                              quantity=0,
                              account_id=already_out_account_id)
            credit_line = dict(base_line,
                               name=(self.name + ": " + str(qty_out) + _(' already out')),
                               quantity=0,
                               account_id=debit_account_id)
            diff = diff * qty_out / self.quantity
            if diff > 0:
                debit_line['debit'] = diff
                credit_line['credit'] = diff
                if self.cost_id and self.cost_id.other_currency:
                    debit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                    debit_line['amount_currency'] = diff
                    debit_line['debit'] = diff * self.cost_id.exchange_rate
                    credit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                    credit_line['amount_currency'] = diff * -1
                    credit_line['credit'] = diff * self.cost_id.exchange_rate
            else:
                # negative cost, reverse the entry
                debit_line['credit'] = -diff
                credit_line['debit'] = -diff
                if self.cost_id and self.cost_id.other_currency:
                    debit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                    debit_line['amount_currency'] = -diff
                    debit_line['debit'] = -diff * self.cost_id.exchange_rate
                    credit_line['currency_id'] = self.currency_id and self.currency_id.id or False
                    credit_line['amount_currency'] = diff
                    credit_line['credit'] = -diff * self.cost_id.exchange_rate
            AccountMoveLine.append([0, 0, debit_line])
            AccountMoveLine.append([0, 0, credit_line])

            # TDE FIXME: oh dear
            if self.env.user.company_id.anglo_saxon_accounting:
                debit_line = dict(base_line,
                                  name=(self.name + ": " + str(qty_out) + _(' already out')),
                                  quantity=0,
                                  account_id=credit_account_id)
                credit_line = dict(base_line,
                                   name=(self.name + ": " + str(qty_out) + _(' already out')),
                                   quantity=0,
                                   account_id=already_out_account_id)

                if diff > 0:
                    debit_line['debit'] = diff
                    credit_line['credit'] = diff
                else:
                    # negative cost, reverse the entry
                    debit_line['credit'] = -diff
                    credit_line['debit'] = -diff
                AccountMoveLine.append([0, 0, debit_line])
                AccountMoveLine.append([0, 0, credit_line])

        return AccountMoveLine

    @api.multi
    def _calculated_cost(self):
        for rec in self:
            if not rec.calculated_cost:
                account_id = rec.product_id.property_account_expense_id or \
                             rec.product_id.categ_id.property_account_expense_categ_id
                if not account_id:
                    account_id = self.env['account.account'].search([], limit=1)
                total = rec.product_id.qty_available * rec.product_id.standard_price
                if rec.cost_id.other_currency:
                    total = total + (rec.additional_landed_cost * rec.cost_id.exchange_rate)
                else:
                    total = total + rec.additional_landed_cost
                if rec.product_id.qty_available:
                    total = total / rec.product_id.qty_available
                rec.product_id.do_change_standard_price(total, account_id.id)
                rec.historical_cost = rec.product_id.standard_price
                rec.calculated_cost = True

    @api.multi
    @api.depends('additional_landed_cost_final', 'currency_id', 'cost_id.exchange_rate', 'cost_id.exchange_date')
    def _compute_additional_landed_cost_final(self):
        for rec in self:
            if rec.cost_id.other_currency:
                rec.additional_landed_cost_final = (rec.additional_landed_cost * rec.cost_id.exchange_rate)
            else:
                rec.additional_landed_cost_final = rec.additional_landed_cost

# class StockLandedCostRegistry(models.Model):
#     _name = "stock.landed.cost.registry"
#     _description = "Registro de los movimientos de Gastos de Envío"
#
#     name = fields.Char(string="Nombre")
#     product_id = fields.Many2one('product.product', string='Product')
#
#
# class StockMoveLine(models.Model):
#     _inherit = "stock.move.line"
#
#     registry_id = fields.Many2one(comodel_name='stock.landed.cost.registry', string='Registry')
#
#
# class StockLandedCostRegistry(models.Model):
#     _inherit = "stock.landed.cost.registry"
#
#     registry_id = fields
#
#     @api.model
#     def create_registry(self, product):
#         stock_move_line_obj = self.env['stock.move.line']
#         if product:
#             domain = [
#                 ('state', 'like', 'done'),
#                 ('product_id', '=', product.id)
#             ]
#             stock_move_line = stock_move_line_obj.search(domain)
