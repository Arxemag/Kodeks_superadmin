import { baseApi } from '~/shared/api/baseApi';

export const ssoApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    createCabinetSsoLink: builder.mutation<{ url: string }, { link: string }>({
      query: (body) => ({ url: '/expert/sso-link', method: 'POST', body }),
    }),
  }),
});

export const { useCreateCabinetSsoLinkMutation } = ssoApi;
