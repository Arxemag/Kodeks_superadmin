import { HttpService } from '@nestjs/axios';
import { Body, Controller, HttpException, Post, Req, Res, ServiceUnavailableException, UnauthorizedException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { DataSource } from 'typeorm';
import { firstValueFrom } from 'rxjs';
import type { Request, Response } from 'express';
import { GetCurrentUser } from '@app/common';
import { ROLE, STATUS_USER } from '@app/const';
import { UserCompanyEntity } from '../../user-company/domain/user-company.entity';
import type { Actor } from './sso.policy';
import { SsoPolicyError, authCookie, companyIdentity, hostAliases, issuerUrl, readLink, registryForHost, registryRows, roleKind, supportUid } from './sso.policy';

@Controller('expert')
export class ExpertSsoController {
  constructor(private readonly http: HttpService, private readonly config: ConfigService, private readonly dataSource: DataSource) {}

  @Post('sso-link')
  async create(@Body() body: unknown, @GetCurrentUser() actor: Actor,
               @Req() request: Request, @Res({ passthrough: true }) response: Response): Promise<{ url: string }> {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Referrer-Policy', 'no-referrer');
    try {
      const target = readLink(body);
      const kind = roleKind(actor?.role, [ROLE.ADMIN_COMPANY, ROLE.CURRENT, ROLE.POTENTIAL],
                            [ROLE.ADMIN, ROLE.BRIGADIER, ROLE.MOO, ROLE.MOP]);
      const cookie = authCookie(request.cookies?.Auth);
      const base = this.config.get<string>('EXPERT_BASE_URL')?.replace(/\/+$/, '');
      const secret = this.config.get<string>('EXPERT_SSO_API_TOKEN');
      if (!base || !secret) throw new ServiceUnavailableException('Сервис входа не настроен');
      const options = { timeout: 10000, headers: { 'X-Api-Token': secret } };
      const registry = registryRows((await firstValueFrom(this.http.get(`${base}/reg-services`, options))).data);
      let reg: string | undefined;
      let uid: string;
      if (kind === 'company') {
        const account = await this.dataSource.getRepository(UserCompanyEntity).findOne({
          where: { id: Number(actor.userId) }, relations: { user: true, company: { reg: true } },
        });
        const identity = companyIdentity(actor, account, STATUS_USER.FIRED);
        reg = identity.reg;
        uid = identity.uid;
      } else {
        uid = supportUid(cookie);
      }
      const row = registryForHost(target, registry, hostAliases(this.config.get<string>('EXPERT_SSO_HOST_ALIASES')), reg);
      const payload = { reg: row.reg_number, uid, path: target.pathname + target.search + target.hash, cookies: { Auth: cookie } };
      const reply = await firstValueFrom(this.http.post(`${base}/sso-ticket`, payload, { ...options, timeout: 45000 }));
      return { url: issuerUrl(reply.data?.url, row) };
    } catch (error) {
      if (error instanceof SsoPolicyError) throw new HttpException(error.message, error.status);
      if (error instanceof HttpException) throw error;
      // Axios errors may contain the Auth cookie and API token. Never log or
      // rethrow their request/response data through the portal exception filter.
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 401 || status === 403) throw new UnauthorizedException('Сессия каталога истекла. Войдите в портал заново');
      throw new ServiceUnavailableException('Вход в кабинет временно недоступен');
    }
  }
}
