<?php

namespace App\Controller;

use App\Entity\RefreshToken;
use App\Entity\User;
use Doctrine\ORM\EntityManagerInterface;
use Lexik\Bundle\JWTAuthenticationBundle\Services\JWTTokenManagerInterface;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\Cookie;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\Routing\Attribute\Route;

class SecurityController extends AbstractController
{
    private const JWT_COOKIE_TTL = 3600;
    private const REFRESH_COOKIE_TTL = 604800; // 7 days

    /** Handled entirely by LexikJWT's json_login firewall listener. */
    #[Route('/auth', name: 'app_login_jwt', methods: ['POST'])]
    public function jwtLogin(): Response
    {
    }

    /**
     * Validates the refresh_token cookie, issues a fresh JWT cookie, and rotates
     * the refresh token. Returns 401 if the token is missing, unknown, or expired.
     */
    #[Route('/auth/refresh', name: 'app_refresh_token', methods: ['POST'])]
    public function refreshToken(
        Request $request,
        EntityManagerInterface $em,
        JWTTokenManagerInterface $jwtManager,
    ): JsonResponse {
        $tokenString = $request->cookies->get('refresh_token');
        if (!$tokenString) {
            return $this->json(['message' => 'No refresh token'], Response::HTTP_UNAUTHORIZED);
        }

        $refreshToken = $em->getRepository(RefreshToken::class)->findOneBy(['token' => $tokenString]);
        if (!$refreshToken || $refreshToken->isExpired()) {
            if ($refreshToken) {
                $em->remove($refreshToken);
                $em->flush();
            }
            return $this->json(['message' => 'Invalid or expired refresh token'], Response::HTTP_UNAUTHORIZED);
        }

        $user = $refreshToken->getUser();
        $secure = $request->isSecure();

        // Rotate the refresh token before issuing the new JWT.
        $newRefreshString = bin2hex(random_bytes(32));
        $refreshToken->setToken($newRefreshString)
                     ->setExpiresAt(new \DateTimeImmutable(sprintf('+%d seconds', self::REFRESH_COOKIE_TTL)));
        $em->flush();

        $newJwt = $jwtManager->create($user);
        $response = $this->json(['success' => true]);

        $response->headers->setCookie(new Cookie(
            name: 'jwt_token',
            value: $newJwt,
            expire: time() + self::JWT_COOKIE_TTL,
            path: '/',
            domain: null,
            secure: $secure,
            httpOnly: true,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));

        $response->headers->setCookie(new Cookie(
            name: 'refresh_token',
            value: $newRefreshString,
            expire: time() + self::REFRESH_COOKIE_TTL,
            path: '/auth',
            domain: null,
            secure: $secure,
            httpOnly: true,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));

        $response->headers->setCookie(new Cookie(
            name: 'auth_status',
            value: '1',
            expire: time() + self::REFRESH_COOKIE_TTL,
            path: '/',
            domain: null,
            secure: $secure,
            httpOnly: false,
            raw: false,
            sameSite: Cookie::SAMESITE_STRICT,
        ));

        return $response;
    }

    /**
     * Deletes the refresh token from the database and clears all three auth cookies.
     * Safe to call even when already logged out (no-op if token is unknown).
     */
    #[Route('/auth/logout', name: 'app_logout', methods: ['POST'])]
    public function logout(Request $request, EntityManagerInterface $em): JsonResponse
    {
        $tokenString = $request->cookies->get('refresh_token');
        if ($tokenString) {
            $refreshToken = $em->getRepository(RefreshToken::class)->findOneBy(['token' => $tokenString]);
            if ($refreshToken) {
                $em->remove($refreshToken);
                $em->flush();
            }
        }

        $secure = $request->isSecure();
        $response = $this->json(['success' => true]);

        $response->headers->clearCookie('jwt_token', '/', null, $secure, true, Cookie::SAMESITE_STRICT);
        $response->headers->clearCookie('refresh_token', '/auth', null, $secure, true, Cookie::SAMESITE_STRICT);
        $response->headers->clearCookie('auth_status', '/', null, $secure, false, Cookie::SAMESITE_STRICT);

        return $response;
    }
}
