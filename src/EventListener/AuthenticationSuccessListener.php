<?php

namespace App\EventListener;

use App\Entity\RefreshToken;
use App\Entity\User;
use Doctrine\ORM\EntityManagerInterface;
use Lexik\Bundle\JWTAuthenticationBundle\Event\AuthenticationSuccessEvent;
use Lexik\Bundle\JWTAuthenticationBundle\Events;
use Symfony\Component\DependencyInjection\Attribute\Autowire;
use Symfony\Component\EventDispatcher\Attribute\AsEventListener;
use Symfony\Component\HttpFoundation\Cookie;
use Symfony\Component\HttpFoundation\RequestStack;

/**
 * After LexikJWT creates a token response for a successful /auth login,
 * this listener also writes it (and a refresh token) as httpOnly cookies so
 * browser clients don't need to manage the token in JS storage.
 *
 * Three cookies are set:
 *   jwt_token     — httpOnly, lives for app.jwt_ttl, matching the JWT's own TTL
 *   refresh_token — httpOnly, lives for app.refresh_token_ttl, path /auth,
 *                   rotated on every call to /auth/refresh
 *   auth_status   — NOT httpOnly, same lifetime as the refresh token;
 *                   JS reads this to detect a live session
 *
 * The JSON body still contains the token so that non-browser API clients
 * and the test suite (which use the Authorization header) continue to work.
 */
#[AsEventListener(event: Events::AUTHENTICATION_SUCCESS, method: 'onAuthenticationSuccess')]
class AuthenticationSuccessListener
{
    public function __construct(
        private EntityManagerInterface $em,
        private RequestStack $requestStack,
        #[Autowire('%app.jwt_ttl%')] private readonly int $jwtTtl,
        #[Autowire('%app.refresh_token_ttl%')] private readonly int $refreshTokenTtl,
    ) {}

    public function onAuthenticationSuccess(AuthenticationSuccessEvent $event): void
    {
        $user = $event->getUser();
        if (!$user instanceof User) {
            return;
        }

        $jwt = $event->getData()['token'];
        $response = $event->getResponse();
        $secure = $this->requestStack->getCurrentRequest()?->isSecure() ?? false;

        $response->headers->setCookie(new Cookie(
            name: 'jwt_token',
            value: $jwt,
            expire: time() + $this->jwtTtl,
            path: '/',
            domain: null,
            secure: $secure,
            httpOnly: true,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));

        $refreshTokenString = bin2hex(random_bytes(RefreshToken::TOKEN_BYTES));
        $refreshToken = (new RefreshToken())
            ->setToken($refreshTokenString)
            ->setUser($user)
            ->setExpiresAt(new \DateTimeImmutable(sprintf('+%d seconds', $this->refreshTokenTtl)));

        $this->em->persist($refreshToken);
        $this->em->flush();

        $response->headers->setCookie(new Cookie(
            name: 'refresh_token',
            value: $refreshTokenString,
            expire: time() + $this->refreshTokenTtl,
            path: '/auth',
            domain: null,
            secure: $secure,
            httpOnly: true,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));

        // Non-httpOnly flag cookie so JS can detect an active session without
        // touching the JWT itself.
        $response->headers->setCookie(new Cookie(
            name: 'auth_status',
            value: '1',
            expire: time() + $this->refreshTokenTtl,
            path: '/',
            domain: null,
            secure: $secure,
            httpOnly: false,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));
    }
}
