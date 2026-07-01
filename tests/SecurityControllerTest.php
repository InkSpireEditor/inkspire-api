<?php

namespace App\Tests;

use App\Entity\RefreshToken;
use Symfony\Component\HttpFoundation\Response;

/**
 * Covers the cookie-based auth lifecycle added in S-02/S-10:
 * login sets httpOnly cookies, /auth/refresh rotates the refresh token and
 * issues a fresh JWT, and /auth/logout revokes the refresh token.
 *
 * AuthenticatedWebTestCase::setUp already performs a login for the test user,
 * so on entry the client holds the jwt_token / refresh_token / auth_status
 * cookies and exactly one RefreshToken row exists.
 */
class SecurityControllerTest extends AuthenticatedWebTestCase
{
    /** @return string[] names of Set-Cookie entries on the last response */
    private function responseCookieNames(): array
    {
        return array_map(
            static fn ($cookie) => $cookie->getName(),
            $this->client->getResponse()->headers->getCookies()
        );
    }

    public function test_01_loginSetsAuthCookiesAndPersistsRefreshToken(): void
    {
        // The login in setUp is the most recent response.
        $names = $this->responseCookieNames();
        $this->assertContains('jwt_token', $names);
        $this->assertContains('refresh_token', $names);
        $this->assertContains('auth_status', $names);

        $repo = $this->entityManager->getRepository(RefreshToken::class);
        $this->assertCount(1, $repo->findAll());

        // The jwt_token cookie must be httpOnly; auth_status must not be.
        foreach ($this->client->getResponse()->headers->getCookies() as $cookie) {
            if ($cookie->getName() === 'jwt_token') {
                $this->assertTrue($cookie->isHttpOnly(), 'jwt_token must be httpOnly');
                $this->assertSame('strict', $cookie->getSameSite());
            }
            if ($cookie->getName() === 'auth_status') {
                $this->assertFalse($cookie->isHttpOnly(), 'auth_status must be readable by JS');
            }
        }
    }

    public function test_02_refreshIssuesNewJwtAndRotatesToken(): void
    {
        $repo = $this->entityManager->getRepository(RefreshToken::class);
        $before = $repo->findAll();
        $this->assertCount(1, $before);
        $oldToken = $before[0]->getToken();

        // The refresh_token cookie (path /auth) is sent automatically.
        $this->client->request('POST', '/auth/refresh');
        $this->assertResponseIsSuccessful();

        $this->assertContains('jwt_token', $this->responseCookieNames());
        $this->assertContains('refresh_token', $this->responseCookieNames());

        // Same single row, new token value (rotation).
        $this->entityManager->clear();
        $after = $repo->findAll();
        $this->assertCount(1, $after);
        $this->assertNotEquals($oldToken, $after[0]->getToken());
    }

    public function test_03_refreshWithoutCookieIsUnauthorized(): void
    {
        $this->client->getCookieJar()->clear();
        $this->client->request('POST', '/auth/refresh');
        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
    }

    public function test_04_refreshWithUnknownTokenIsUnauthorized(): void
    {
        $this->client->getCookieJar()->clear();
        $this->client->getCookieJar()->set(
            new \Symfony\Component\BrowserKit\Cookie('refresh_token', 'not-a-real-token')
        );
        $this->client->request('POST', '/auth/refresh');
        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
    }

    public function test_05_logoutRevokesRefreshTokenAndClearsCookies(): void
    {
        $repo = $this->entityManager->getRepository(RefreshToken::class);
        $this->assertCount(1, $repo->findAll());

        $this->client->request('POST', '/auth/logout');
        $this->assertResponseIsSuccessful();

        // Refresh token row is gone.
        $this->entityManager->clear();
        $this->assertCount(0, $repo->findAll());

        // Auth cookies are cleared (Set-Cookie with an expiry in the past).
        $cleared = [];
        foreach ($this->client->getResponse()->headers->getCookies() as $cookie) {
            if ($cookie->getExpiresTime() !== 0 && $cookie->getExpiresTime() < time()) {
                $cleared[] = $cookie->getName();
            }
        }
        $this->assertContains('jwt_token', $cleared);
        $this->assertContains('refresh_token', $cleared);
        $this->assertContains('auth_status', $cleared);
    }
}
